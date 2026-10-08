#!/usr/bin/env python3
"""agent_harness 的离线端到端调试自检。

覆盖真实 host 主循环、工具检查、目标 Stop 边界、任务生命周期、
工作流恢复、Git worktree 和子 agent 生命周期，全程不联网。

  PYTHONPATH=src python scripts/debug_pass.py

所有检查都在新建临时目录中运行，不影响仓库及其运行时状态。
退出码等于失败检查的数量。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

os.environ.setdefault("MODEL_ID", "debug-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "debug-key")
os.environ.setdefault("LLM_API", "responses")

# `compact` 声明为工具，但由 agent_loop 内部处理，处理器表中没有对应项。
# 其他 schema 都必须有处理器。
INLINE_ONLY_TOOLS = {"compact"}

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    started = time.time()
    try:
        detail = fn()
        RESULTS.append(("PASS", name, f"{time.time() - started:.2f}s  {detail or ''}".rstrip()))
    except Exception as exc:  # 单项失败不应中断后续检查
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
    """在独立进程中执行 worktree 流程，此时 cwd 是 Git 仓库。"""
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
    # 任务仍在进行时不允许移除 worktree，必须先完成任务。
    assert "Error" in host.remove_worktree("dbg", discard_changes=True)
    assert "Completed" in host.complete_task(task.id, owner="agent")
    assert host.BUILTIN_HANDLERS["remove_worktree"](name="dbg").startswith("Worktree")
    assert "agent" not in host.assignments
    assert host.load_task(task.id).worktree is None
    assert host.assignment_cwd("agent") == repo
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

    host.remember_after_turn = lambda messages: None  # 保持记忆功能离线

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
        host.core.client = client
        messages = [{"role": "user", "content": "write hello.txt"}]
        host.agent_loop(messages, "write hello.txt")
        assert (workdir / "hello.txt").read_text(encoding="utf-8") == "hi from agent\n"
        assert len(client.calls) == 2, f"expected 2 model calls, got {len(client.calls)}"
        assert client.calls[0]["model"] == host.core.MODEL
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
        host.core.client = client
        host.GOAL = goal_module.GoalController(evaluator=Evaluator())
        host.GOAL.set_goal("tests pass")
        messages = [{"role": "user", "content": "go"}]
        host.agent_loop(messages, "go")
        assert len(client.calls) == 2, f"expected 2 model calls, got {len(client.calls)}"
        injected = [m for m in messages if "[Goal still active]" in str(m.get("content"))]
        assert len(injected) == 1, "block message not injected exactly once"
        assert host.GOAL.active is None, "goal not cleared after success"
        return "block -> continue -> allow -> cleared"

    def t_tasks():
        host.assignments.clear()
        task = host.create_task("debug task")
        assert "Claimed" in host.claim_task(task.id, owner="alice")
        assert "Completed" in host.complete_task(task.id, owner="alice")
        assert host.release_completed_assignment("alice") is True
        assert "alice" not in host.assignments
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
        wf = host.workflow_runtime
        original_factory = wf.RUNNER_FACTORY
        async def write_step(ctx, args):
            return await ctx.agent("Write workflow-child.txt", label="debug write")
        wf.WORKFLOWS["debug-write"] = ({"name": "debug-write", "description": "offline write"}, write_step)
        client = FakeClient([
            tool_response("write_file", {"path": "workflow-child.txt", "content": "saved"}),
            text_response("Saved the result."),
        ])
        wf.RUNNER_FACTORY = lambda: wf.SubagentRunner(client, "debug-model")
        try:
            handler = host.assemble_tool_pool()[1]["Workflow"]
            out = json.loads(handler(name="debug-write"))
            replay = json.loads(handler(name="debug-write", resume_from_run_id=out["task"]["runId"]))
            assert out["task"]["status"] == "completed", out
            assert replay["task"]["usage"] == {"agents": 0, "tokens": 0}, replay
            assert len(client.calls) == 2 and not host.active_subagents
            assert (workdir / "workflow-child.txt").read_text() == "saved"
        finally:
            wf.RUNNER_FACTORY = original_factory
            wf.WORKFLOWS.pop("debug-write", None)
        return "demo/resume; unified child write and cache replay"

    def t_subagents():
        done = threading.Event()
        original_thread = threading.Thread
        def thread_factory(target, **kwargs):
            def run():
                try:
                    target()
                finally:
                    done.set()
            return original_thread(target=run, **kwargs)
        host.subagent.threading.Thread = thread_factory
        host.core.client = FakeClient([
            text_response("Investigation complete."),
            tool_response("write_file", {"path": "child.txt", "content": "child result"}),
            text_response("Persistent work complete."),
        ])
        try:
            create = host.BUILTIN_HANDLERS["spawn_subagent"]
            once = json.loads(create(name="scout", prompt="Inspect", mode="once"))
            assert once["status"] == "completed" and "scout" not in host.active_subagents
            started = json.loads(create(name="worker", prompt="Write child.txt", mode="persistent"))
            assert started["status"] == "started"
            events = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any(
                message["type"] == "idle_notification" for message in events
            ):
                events.extend(host.BUS.wait_for_messages("lead", deadline - time.monotonic()))
            assert any(message["type"] == "result" for message in events), events
            assert any(message["type"] == "idle_notification" for message in events), events
            assert (workdir / "child.txt").read_text(encoding="utf-8") == "child result"
            assert host.active_subagents["worker"].status == "idle"
            host.run_request_shutdown("worker")
            assert done.wait(3), "child failed to stop"
            assert "worker" not in host.active_subagents
            assert any(message["type"] == "shutdown_response"
                       for message in host.consume_lead_inbox()), "no shutdown acknowledgement"
            return "once result; persistent start/write/events/shutdown"
        finally:
            if "worker" in host.active_subagents:
                host.run_request_shutdown("worker")
                done.wait(3)
            host.subagent.threading.Thread = original_thread

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

    check("tool pool assembled", t_tool_pool)
    check("agent loop: tool call round-trip", t_loop_toolcall)
    check("edit_file: mismatch guarded", t_edit_guard)
    check("read_file: path escape blocked", t_escape)
    check("goal: stop boundary blocks once", t_goal_boundary)
    check("task lifecycle", t_tasks)
    check("unified subagent lifetimes", t_subagents)
    check("workflow demo/resume offline", t_workflow)
    check("worktree git ops", t_worktree)

    failures = sum(1 for status, _, _ in RESULTS if status == "FAIL")
    print("\n=== DEBUG REPORT ===")
    for status, name, detail in RESULTS:
        print(f"[{status}] {name} :: {detail}")
    print(f"\n{len(RESULTS) - failures}/{len(RESULTS)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(main())
