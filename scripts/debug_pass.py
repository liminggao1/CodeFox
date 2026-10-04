#!/usr/bin/env python3
"""Offline end-to-end debug pass for agent_harness.

Exercises the real host loop, tool guards, goal Stop boundary, task lifecycle,
workflow resume, git worktrees, and the flat build - all without network.

  PYTHONPATH=src python scripts/debug_pass.py

Everything runs inside a fresh temp directory, so the repo and its runtime state
are untouched. Exit code is the number of failed checks.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
FLAT_FILE = PROJECT_ROOT / "flat" / "agent_full.py"

os.environ.setdefault("MODEL_ID", "debug-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "debug-key")
os.environ.setdefault("LLM_API", "responses")

# `compact` is declared as a tool but handled inline by agent_loop, so it has no
# entry in the handler table. Every other schema must have a handler.
INLINE_ONLY_TOOLS = {"compact"}

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    started = time.time()
    try:
        detail = fn()
        RESULTS.append(("PASS", name, f"{time.time() - started:.2f}s  {detail or ''}".rstrip()))
    except Exception as exc:  # a failing check must not stop the pass
        RESULTS.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))


class FakeMessages:
    def __init__(self, outer):
        self._outer = outer

    def create(self, **kwargs):
        self._outer.calls.append(kwargs)
        if not self._outer.responses:
            raise AssertionError("no scripted response left")
        return self._outer.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []
        self.messages = FakeMessages(self)


def text_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def tool_response(name: str, arguments: dict, tool_use_id: str = "tool-1"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_use_id, name=name, input=arguments)],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def run_git(*args: str, cwd: Path):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def worktree_child() -> int:
    """Drive the worktree flow in its own process, where cwd == the git repo."""
    import agent_harness.host as host

    repo = Path.cwd()
    task = host.create_task("worktree debug")
    assert host.create_worktree("dbg", task.id).startswith("Worktree"), "create failed"
    assert "Claimed" in host.claim_task(task.id, owner="agent")
    bound = (repo / ".worktrees" / "dbg").resolve()
    cwd, error = host.task_worktree_cwd(host.load_task(task.id))
    assert error is None and cwd == bound, (cwd, error)
    (bound / "b.txt").write_text("work done\n", encoding="utf-8")
    run_git("add", "-A", cwd=bound)
    run_git("commit", "-qm", "work in worktree", cwd=bound)
    # Removal is refused while the task is still active; complete it first.
    assert "Error" in host.remove_worktree("dbg", discard_changes=True)
    assert "Completed" in host.complete_task(task.id, owner="agent")
    assert host.release_completed_assignment("agent") is True
    assert host.remove_worktree("dbg", discard_changes=True).startswith("Worktree")
    listing = run_git("worktree", "list", cwd=repo).stdout
    assert ".worktrees/dbg" not in listing, listing
    print("WORKTREE FLOW OK")
    return 0


def main() -> int:
    if "--worktree-child" in sys.argv:
        return worktree_child()

    sys.path.insert(0, str(SRC_DIR))
    workdir = Path(tempfile.mkdtemp(prefix="agent-harness-debug-"))
    os.chdir(workdir)

    import agent_harness.host as host
    from agent_harness import goal as goal_module

    host.remember_after_turn = lambda messages: None  # keep memory offline
    context = host.update_context({}, [])

    def t_tool_pool():
        tools, handlers = host.assemble_tool_pool()
        names = [tool["name"] for tool in tools]
        assert len(names) == len(set(names)), "duplicate tool names"
        assert "write_file" in names and "Workflow" in names, names
        missing = set(names) - set(handlers) - INLINE_ONLY_TOOLS
        assert not missing, f"schema without handler: {sorted(missing)}"
        assert not set(handlers) - set(names), "handler without schema"
        return f"{len(names)} tools ({len(handlers)} handlers + {sorted(INLINE_ONLY_TOOLS)})"

    def t_loop_toolcall():
        client = FakeClient([
            tool_response("write_file", {"path": "hello.txt", "content": "hi from agent\n"}),
            text_response("Wrote the file."),
        ])
        host.client = client
        messages = [{"role": "user", "content": "write hello.txt"}]
        host.agent_loop(messages, dict(context), "write hello.txt")
        assert (workdir / "hello.txt").read_text(encoding="utf-8") == "hi from agent\n"
        assert len(client.calls) == 2, f"expected 2 model calls, got {len(client.calls)}"
        assert client.calls[0]["model"] == host.MODEL
        assert messages[-1]["role"] == "assistant"
        return "tool round-trip, 2 model calls"

    def t_edit_guard():
        (workdir / "e.txt").write_text("alpha beta\n", encoding="utf-8")
        _, handlers = host.assemble_tool_pool()
        out = host.call_tool_handler(
            handlers["edit_file"],
            {"path": "e.txt", "old_text": "nope", "new_text": "x"},
            "edit_file",
        )
        assert (workdir / "e.txt").read_text(encoding="utf-8") == "alpha beta\n", "file changed"
        assert "error" in str(out).lower(), out
        host.call_tool_handler(
            handlers["edit_file"],
            {"path": "e.txt", "old_text": "beta", "new_text": "gamma"},
            "edit_file",
        )
        assert (workdir / "e.txt").read_text(encoding="utf-8") == "alpha gamma\n"
        return "mismatch refused, then applied"

    def t_escape():
        _, handlers = host.assemble_tool_pool()
        out = host.call_tool_handler(
            handlers["read_file"], {"path": "../../../etc/passwd"}, "read_file"
        )
        assert "Error" in str(out), out
        return "path outside workdir blocked"

    def t_goal_boundary():
        class Evaluator:
            def __init__(self):
                self.seen = 0

            async def evaluate(self, condition, messages):
                self.seen += 1
                if self.seen == 1:
                    return goal_module.GoalEvaluation(False, "no test output yet")
                return goal_module.GoalEvaluation(True, "tests pass")

        client = FakeClient([text_response("Done."), text_response("Verified.")])
        host.client = client
        host.GOAL = goal_module.GoalController(evaluator=Evaluator())
        host.GOAL.set_goal("tests pass")
        messages = [{"role": "user", "content": "go"}]
        host.agent_loop(messages, dict(context), "go")
        assert len(client.calls) == 2, f"expected 2 model calls, got {len(client.calls)}"
        injected = [m for m in messages if "[Goal still active]" in str(m.get("content"))]
        assert len(injected) == 1, "block message not injected exactly once"
        assert host.GOAL.active is None, "goal not cleared after success"
        return "block -> continue -> allow -> cleared"

    def t_tasks():
        host.teammate_assignments.clear()
        task = host.create_task("debug task")
        assert "Claimed" in host.claim_task(task.id, owner="alice")
        assert "Completed" in host.complete_task(task.id, owner="alice")
        assert host.release_completed_assignment("alice") is True
        assert "alice" not in host.teammate_assignments
        return "create/claim/complete/release"

    def t_workflow():
        env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        first = subprocess.run(
            [sys.executable, "-m", "agent_harness", "workflow", "demo"],
            cwd=workdir, capture_output=True, text=True, timeout=180, env=env,
        )
        assert first.returncode == 0, first.stderr
        assert "status=completed" in first.stdout, first.stdout
        resumed = subprocess.run(
            [sys.executable, "-m", "agent_harness", "workflow", "resume"],
            cwd=workdir, capture_output=True, text=True, timeout=180, env=env,
        )
        assert resumed.returncode == 0, resumed.stderr
        assert "status=completed  agents=0  tokens=0" in resumed.stdout, resumed.stdout
        assert (workdir / ".runtime").is_dir()
        return "demo, journal cache hit, state in launch dir"

    def t_worktree():
        repo = workdir / "gitrepo"
        repo.mkdir()
        run_git("init", "-q", cwd=repo)
        run_git("config", "user.email", "debug@example.com", cwd=repo)
        run_git("config", "user.name", "debug", cwd=repo)
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-qm", "init", cwd=repo)
        env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        result = subprocess.run(
            [sys.executable, __file__, "--worktree-child"],
            cwd=repo, capture_output=True, text=True, timeout=180, env=env,
        )
        assert result.returncode == 0, result.stderr[-400:]
        assert "WORKTREE FLOW OK" in result.stdout
        return "create/claim/commit/complete/remove"

    def t_flat():
        spec = importlib.util.spec_from_file_location(
            "build_flat", PROJECT_ROOT / "scripts" / "build_flat.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.build() == FLAT_FILE.read_text(encoding="utf-8"), "flat file is stale"
        result = subprocess.run(
            [sys.executable, str(FLAT_FILE), "workflow", "demo"],
            cwd=workdir, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stderr[-400:]
        assert "status=completed" in result.stdout
        return "in sync with sources and runs standalone"

    check("tool pool assembled", t_tool_pool)
    check("agent loop: tool call round-trip", t_loop_toolcall)
    check("edit_file: mismatch guarded", t_edit_guard)
    check("read_file: path escape blocked", t_escape)
    check("goal: stop boundary blocks once", t_goal_boundary)
    check("task lifecycle", t_tasks)
    check("workflow demo/resume offline", t_workflow)
    check("worktree git ops", t_worktree)
    check("flat build freshness", t_flat)

    failures = sum(1 for status, _, _ in RESULTS if status == "FAIL")
    print("\n=== DEBUG REPORT ===")
    for status, name, detail in RESULTS:
        print(f"[{status}] {name} :: {detail}")
    print(f"\n{len(RESULTS) - failures}/{len(RESULTS)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(main())
