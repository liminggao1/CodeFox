#!/usr/bin/env python3
"""构建 flat/agent_full.py：把整个框架汇总为一个可读文件。

按依赖顺序拼接源码模块，并移除模块头、导入语句和 __main__ 代码块。
相对导入替换为 ``sys.modules[__name__]`` 别名，使拼接后的代码仍可作为
单个模块运行。

可从任意目录运行：python scripts/build_flat.py
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = PROJECT_ROOT / "src" / "agent_harness"
OUTPUT = PROJECT_ROOT / "flat" / "agent_full.py"

# 拼接顺序遵循包的导入顺序；host 部分最后连接各功能模块。
SECTIONS = [
    "core", "tasks", "worktrees", "skills", "prompts", "file_tools", "teams",
    "hooks", "subagent", "compaction", "recovery", "scheduler",
    "task_worktrees", "mcp_tools",
    "registrations", "memory_runtime", "runtime_context", "goal", "workflow", "host",
]

# Flat 构建把所有模块合并到同一命名空间，所以 "from . import X" 变为本模块的别名。
RELATIVE_IMPORT_REPLACEMENTS = {
    "from .llm_client import Anthropic": "from agent_harness.llm_client import Anthropic",
    "from . import goal as goal_module": "goal_module = sys.modules[__name__]",
    "from . import workflow as workflow_runtime": (
        "workflow_runtime = sys.modules[__name__]"
    ),
}

ENTRY = '''

# ==== flat 入口（来自 cli.py）====


def handle_goal_command(query: str) -> tuple[bool, str]:
    """在本地处理 /goal，返回（是否已处理，替换后的输入）。"""
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
    session_state = {"active_user_request": "(no active user request)"}
    threading.Thread(
        target=async_event_loop,
        args=(history, session_state),
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
            agent_loop(history, query)
            print_turn_assistants(history, turn_start)
        print()


def main(argv: list) -> None:
    global RUNNER_FACTORY
    if argv and argv[0] == "workflow":
        # demo 保持离线；只有交互式 host 会调用 API。
        RUNNER_FACTORY = MockAgentRunner
        asyncio.run(run_demo(argv[1:]))
        return
    run_cli()


if __name__ == "__main__":
    main(sys.argv[1:])
'''


def split_module(source: str) -> tuple[list[str], list[str]]:
    """返回一个模块的（导入行，保留的主体行）。"""
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
    "# 允许从任意目录直接运行此文件。\n"
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
# host 的多行 "from .mod import (a, b, c)" 只用于重新导出名称；
# flat 文件中这些名称已存在，因此导入行可以省略。
MODULE_FROM = re.compile(r"^from \.[A-Za-z_][A-Za-z0-9_.]* import \(.*?\)\n",
                         re.MULTILINE | re.DOTALL)
MODULE_FROM_ONE = re.compile(r"^from \.[A-Za-z_][A-Za-z0-9_.]* import [^(\n]*\n",
                             re.MULTILINE)


def apply_relative_import_replacements(text: str) -> str:
    for old, new in RELATIVE_IMPORT_REPLACEMENTS.items():
        text = text.replace(old, new)
    text = MODULE_FROM.sub("", text)
    text = MODULE_FROM_ONE.sub("", text)
    # 每个功能模块都变成同一个 flat 命名空间的别名。
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
        '"""agent-harness：将所有机制汇总到一个文件。\n\n'
        "由 scripts/build_flat.py 生成；请编辑 src/agent_harness/，不要直接修改本文件。\n"
        "各段对应源码模块，末尾是 CLI 入口。\n"
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
