#!/usr/bin/env python3
"""Build flat/agent_full.py - the whole harness in one readable file.

Concatenates the source modules in dependency order and strips module headers,
imports, and __main__ blocks. Relative imports are replaced by
``sys.modules[__name__]`` aliases, so the concatenated code keeps working as a
single module.

Run from anywhere:  python scripts/build_flat.py
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = PROJECT_ROOT / "src" / "agent_harness"
OUTPUT = PROJECT_ROOT / "flat" / "agent_full.py"

# Concatenation order follows the package import order. The host section runs
# last and wires the feature modules together.
SECTIONS = [
    "core", "tasks", "worktrees", "skills", "prompts", "file_tools", "teams",
    "hooks", "subagent", "compaction", "recovery", "scheduler",
    "task_worktrees", "mcp_tools",
    "registrations", "memory_runtime", "goal", "workflow", "host",
]

# Flat build collapses every module into one namespace, so "from . import X"
# becomes an alias to this very module.
RELATIVE_IMPORT_REPLACEMENTS = {
    "from .llm_client import Anthropic": "from agent_harness.llm_client import Anthropic",
    "from . import memory_runtime as MEMORY_RUNTIME": (
        "MEMORY_RUNTIME = sys.modules[__name__]"
    ),
    "from . import goal as goal_module": "goal_module = sys.modules[__name__]",
    "from . import workflow as workflow_runtime": (
        "workflow_runtime = sys.modules[__name__]"
    ),
}

ENTRY = '''

# ==== flat entry point (from cli.py) ====


def handle_goal_command(query: str) -> tuple[bool, str]:
    """Handle /goal locally. Return (handled, replacement_query)."""
    stripped = query.strip()
    if stripped == "/goal":
        print(GOAL.status(0))
        return True, ""
    if not stripped.startswith("/goal "):
        return False, query

    argument = stripped[6:].strip()
    if not argument:
        print(GOAL.status(0))
        return True, ""
    if argument.lower() in CLEAR_ALIASES:
        print(GOAL.clear())
        return True, ""
    GOAL.set_goal(argument)
    print(f"  \\033[35m[goal] set: {argument}\\033[0m")
    return False, argument


def run_cli() -> None:
    global CLI_ACTIVE
    CLI_ACTIVE = True
    start_runtime_services()
    print("agent-harness (flat): agent loop + tools + workflow + goal")
    print("Enter a question, press Enter to send. Type q to quit.")
    print("Set a completion condition with /goal <condition>.\\n")

    history: list = []
    context = update_context({}, history)
    session_state = {"active_user_request": "(no active user request)"}
    threading.Thread(
        target=async_event_loop,
        args=(history, context, session_state),
        daemon=True,
    ).start()

    while True:
        try:
            query = CONSOLE.ask()
        except (EOFError, KeyboardInterrupt):
            break
        stripped = query.strip()
        if stripped.lower() in ("q", "exit", ""):
            break

        handled, query = handle_goal_command(stripped)
        if handled:
            print()
            continue

        with agent_lock:
            trigger_hooks("UserPromptSubmit", query)
            turn_start = len(history)
            session_state["active_user_request"] = query
            history.append({"role": "user", "content": query})
            agent_loop(history, context, query)
            context = update_context(context, history)
            print_turn_assistants(history, turn_start)
        print()


def main(argv: list) -> None:
    global RUNNER_FACTORY
    if argv and argv[0] == "workflow":
        # The demo path stays offline; only the interactive host calls the API.
        RUNNER_FACTORY = MockAgentRunner
        asyncio.run(run_demo(argv[1:]))
        return
    run_cli()


if __name__ == "__main__":
    main(sys.argv[1:])
'''


def split_module(source: str) -> tuple[list[str], list[str]]:
    """Return (import lines, kept body lines) for one module."""
    tree = ast.parse(source)
    lines = source.splitlines()
    drop_spans = []
    imports: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append("\n".join(lines[node.lineno - 1:node.end_lineno]))
            drop_spans.append((node.lineno, node.end_lineno))
        elif isinstance(node, ast.If) and _is_main_guard(node):
            drop_spans.append((node.lineno, node.end_lineno))
        elif _is_module_docstring(node):
            drop_spans.append((node.lineno, node.end_lineno))

    keep = []
    index = 0
    while index < len(lines):
        lineno = index + 1
        span = next((s for s in drop_spans if s[0] <= lineno <= s[1]), None)
        if span:
            index = span[1]
            continue
        keep.append(lines[index])
        index += 1
    return imports, _trim_blank_edges(keep)


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _is_module_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _trim_blank_edges(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


BOOTSTRAP = (
    "import os\n"
    "import sys\n\n"
    "# Allow running this file directly from any directory.\n"
    "_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
    'sys.path.insert(0, os.path.join(_repo_root, "src"))\n'
)


def collect_imports(modules: dict[str, list[str]]) -> list[str]:
    seen: list[str] = []
    for name in SECTIONS:
        for line in modules[name][0]:
            if line not in seen:
                seen.append(line)
    future = [l for l in seen if l.startswith("from __future__")]
    others = [
        l for l in seen
        if not l.startswith("from __future__") and l not in ("import os", "import sys")
    ]
    return future + [BOOTSTRAP] + others


MODULE_ALIAS = re.compile(r"^from \. import ([A-Za-z_][A-Za-z0-9_]*)(?: as \w+)?$",
                          re.MULTILINE)
# Multi-line "from .mod import (a, b, c)" re-exports of the host section: in the
# flat file those names already exist, so the import line is redundant.
MODULE_FROM = re.compile(r"^from \.[A-Za-z_][A-Za-z0-9_.]* import \(.*?\)\n",
                         re.MULTILINE | re.DOTALL)
MODULE_FROM_ONE = re.compile(r"^from \.[A-Za-z_][A-Za-z0-9_.]* import [^(\n]*\n",
                             re.MULTILINE)


def apply_relative_import_replacements(text: str) -> str:
    for old, new in RELATIVE_IMPORT_REPLACEMENTS.items():
        text = text.replace(old, new)
    text = MODULE_FROM.sub("", text)
    text = MODULE_FROM_ONE.sub("", text)
    # Every feature module becomes an alias to the single flat namespace.
    return MODULE_ALIAS.sub(lambda m: f"{m.group(1)} = sys.modules[__name__]", text)


def build() -> str:
    modules = {
        name: split_module(
            io.open(PACKAGE_DIR / f"{name}.py", encoding="utf-8").read()
        )
        for name in SECTIONS
    }
    header = (
        '#!/usr/bin/env python3\n'
        '"""agent-harness - every mechanism in one flat file.\n\n'
        "Generated by scripts/build_flat.py; edit src/agent_harness/, not\n"
        "this file. Sections follow the source modules, then the CLI entry.\n"
        '"""\n\n'
    )
    parts = [header, "\n".join(collect_imports(modules)), "\n"]
    for name in SECTIONS:
        _, body = modules[name]
        parts.append(f"\n\n# ==== src/agent_harness/{name}.py ====\n\n")
        parts.append("\n".join(body))
        parts.append("\n")
    parts.append(ENTRY)
    return apply_relative_import_replacements("".join(parts))


def main() -> None:
    text = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    io.open(OUTPUT, "w", encoding="utf-8", newline="\n").write(text)
    print(f"wrote {OUTPUT} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
