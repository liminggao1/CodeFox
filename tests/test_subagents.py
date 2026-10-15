"""Host 运行时测试：任务、子 agent、worktree 与创建选项校验。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as S
import os
import subprocess
import sys
import threading

import pytest

import codefox.agents.subagent as subagent_runtime
import codefox.context.compaction as compaction
import codefox.core as core
import codefox.registrations as registrations
import codefox.runtime.hooks as hooks
import codefox.runtime.scheduler as scheduler
import codefox.workspace.task_worktrees as task_worktrees
import codefox.workspace.tasks as tasks
import codefox.workspace.worktrees as worktrees
from support.fakes import response, tool


def spawn_paused(host, monkeypatch, name="alice", **kwargs):
    """Create through the production entry, holding the worker at startup."""
    monkeypatch.setattr(subagent_runtime.threading, "Thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: None))
    result = subagent_runtime.spawn_subagent(name, "Investigate", "persistent",
                                             description="Investigate", **kwargs)
    assert result["status"] == "started", result
    return subagent_runtime.create_subagent_session(name)


def request_id(result):
    return result.split("(")[1].split(")")[0]


def test_assignment_cwd_recovers_plain_and_bound_tasks(
    host_factory, tmp_path, monkeypatch
) -> None:
    host = host_factory()
    plain = tasks.create_task("Plain assignment")
    assert "Claimed" in tasks.claim_task(plain.id, owner="alice")
    tasks.assignments.pop("alice")
    assert tasks.assignment_cwd("alice") == tmp_path
    assert "Completed" in tasks.complete_task(plain.id, owner="alice")
    assert tasks.assignment_cwd("alice") == tmp_path
    assert "alice" in tasks.assignments
    assert tasks.release_completed_assignment("alice") is True
    assert "alice" not in tasks.assignments

    bound = tasks.create_task("Bound assignment")
    bound.worktree = "feature"
    tasks.save_task(bound)
    checkout = tmp_path / ".worktrees" / "feature"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(worktrees, "_registered_worktree",
                        lambda name: (checkout, None))
    assert "Claimed" in tasks.claim_task(bound.id, owner="alice")
    tasks.assignments.pop("alice")
    assert tasks.assignment_cwd("alice") == checkout
    monkeypatch.setattr(worktrees, "_registered_worktree",
                        lambda name: (None, "missing Git registration"))
    with pytest.raises(ValueError, match="missing Git registration"):
        tasks.assignment_cwd("alice")


@pytest.mark.parametrize("owner,archive", [("agent", "tool_output"), ("alice", "transcript")])
def test_bound_file_tools_read_runtime_archives_without_changing_write_cwd(
    host_factory, monkeypatch, owner, archive, no_approval
) -> None:
    host = host_factory()
    checkout = core.WORKDIR / ".worktrees" / "feature"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(worktrees, "_registered_worktree", lambda name: (checkout, None))
    task = tasks.create_task("Read archive")
    task.worktree = "feature"
    tasks.save_task(task)
    assert tasks.claim_task(task.id, owner=owner).startswith("Claimed")
    if archive == "tool_output":
        path = compaction.save_output("archive", "first\n中文 evidence")
    else:
        path = compaction.write_transcript([{"role": "user", "content": "evidence"}])
    expected = path.read_text(encoding="utf-8").rstrip("\n")
    outside = core.WORKDIR / "outside.txt"
    outside.write_text("other root file", encoding="utf-8")
    (checkout / "relative.txt").write_text("task file", encoding="utf-8")

    if owner == "agent":
        read = lambda path: registrations.BUILTIN_HANDLERS["read_file"](path).content
        write = lambda path, content: registrations.BUILTIN_HANDLERS["write_file"](path, content).content
    else:
        session = subagent_runtime.create_subagent_session(owner)
        read = lambda path: session.execute_tool(SimpleNamespace(name="read_file", input={"path": path})).result.content
        write = lambda path, content: session.execute_tool(SimpleNamespace(
            name="write_file", input={"path": path, "content": content})).result.content
    assert read(str(path)) == expected
    assert read("relative.txt") == "task file"
    assert read(str(outside)) == "other root file"
    assert "Wrote" in write(str(path), "replacement")
    assert path.read_text(encoding="utf-8") == "replacement"


def test_cross_process_claim_waits_for_worktree_binding(
    host_factory, tmp_path
) -> None:
    host = host_factory()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "initial",
    ], cwd=tmp_path, check=True)
    task = tasks.create_task("Concurrent binding")
    child_code = (
        "from codefox.workspace import tasks; "
        "print('ready', flush=True); "
        f"print(tasks.claim_task({task.id!r}, owner='alice'), flush=True)"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))

    child = None
    try:
        with tasks.task_store_lock():
            child = subprocess.Popen(
                [sys.executable, "-c", child_code], cwd=tmp_path, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert child.stdout.readline().strip() == "ready"
            assert task_worktrees.create_worktree("concurrent", task.id).startswith("Worktree")
    finally:
        if child is not None:
            stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0, stderr
    assert "Claimed" in stdout
    claimed = tasks.load_task(task.id)
    assert claimed.status == "in_progress"
    assert claimed.worktree == "concurrent"
    assert "Completed" in tasks.complete_task(task.id, owner="alice")
    with pytest.raises(ValueError, match="still in use"):
        task_worktrees.remove_worktree("concurrent")
    assert tasks.release_completed_assignment("alice") is True
    scheduler._background_tasks["bg_test"] = {
        "status": "running", "cwd": str(tmp_path / ".worktrees" / "concurrent"),
    }
    with pytest.raises(ValueError, match="running background"):
        task_worktrees.remove_worktree("concurrent")
    scheduler._background_tasks.clear()
    assert task_worktrees.remove_worktree("concurrent", discard_changes=True).startswith(
        "Worktree"
    )


def test_message_bus_rejects_unregistered_or_unsafe_recipients(
    host_factory, tmp_path
) -> None:
    host = host_factory()
    subagent_runtime._active_subagents["alice"] = subagent_runtime.SubagentRecord("alice", status="idle")

    with pytest.raises(ValueError):
        subagent_runtime._BUS.send("alice", "../escape", "bad")
    assert not (tmp_path / "escape.jsonl").exists()

    with pytest.raises(ValueError) as error:
        subagent_runtime.send_message("alice", "ghost", "Are you there?")
    result = str(error.value)
    assert "not active" in result
    assert not (subagent_runtime._mailbox_dir() / "ghost.jsonl").exists()


def test_plan_gate_blocks_mutating_subagent_tools(host_factory, monkeypatch, tmp_path, no_approval) -> None:
    host = host_factory()
    session = spawn_paused(host, monkeypatch)
    (tmp_path / "config.py").write_text("VALUE = 0")
    cases = {
        "write_file": {"path": "config.py", "content": "VALUE = 1"},
        "edit_file": {"path": "config.py", "old_text": "1", "new_text": "2"},
    }
    for tool_name, tool_input in cases.items():
        block = SimpleNamespace(name=tool_name, input=tool_input)
        subagent_runtime.run_request_plan("alice", "Make the change").content
        submission = request_id(subagent_runtime.submit_plan("alice", "Read, change, test"))
        before = (tmp_path / "config.py").read_text()
        assert "Blocked" in session.execute_tool(block).result.content
        assert (tmp_path / "config.py").read_text() == before
        subagent_runtime.run_review_plan(submission, True).content
        assert "Blocked" in session.execute_tool(block).result.content
        assert subagent_runtime.apply_plan_response("alice", subagent_runtime._BUS.read_inbox("alice")[-1])[0]
        output = session.execute_tool(block).result.content
        assert output.startswith("Wrote" if tool_name == "write_file" else "Edited")


def test_old_approval_cannot_unlock_a_new_assignment(host_factory, monkeypatch) -> None:
    host = host_factory()
    task = tasks.create_task("Review first")
    session = spawn_paused(host, monkeypatch, task_id=task.id)
    submission = subagent_runtime.submit_plan("alice", "1. Inspect\n2. Test")
    request_id = submission.split("(")[1].split(")")[0]
    assert "approved" in subagent_runtime.run_review_plan(request_id, True).content
    response = subagent_runtime._BUS.read_inbox("alice")[-1]

    assert "Completed" in tasks.complete_task(task.id, owner="alice")
    assert tasks.release_completed_assignment("alice")
    next_task = tasks.create_task("Review next")
    assert "Claimed" in tasks.claim_task(next_task.id, owner="alice")
    accepted, notice = subagent_runtime.apply_plan_response("alice", response)

    assert accepted is False
    assert "Ignored" in notice
    block = SimpleNamespace(name="complete_task", input={"task_id": next_task.id})
    assert "Blocked" in session.execute_tool(block).result.content
    assert tasks.load_task(next_task.id).status == "in_progress"


@pytest.mark.parametrize("gate", ["pending", "approved", "rejected"])
def test_requesting_new_plan_invalidates_previous_decisions(host_factory, monkeypatch, gate, no_approval):
    host = host_factory()
    session = spawn_paused(host, monkeypatch)
    old_reply = None
    old_id = request_id(subagent_runtime.submit_plan("alice", "old plan"))
    if gate != "pending":
        assert gate in subagent_runtime.run_review_plan(old_id, gate == "approved").content
        old_reply = subagent_runtime._BUS.read_inbox("alice")[-1]
        assert subagent_runtime.apply_plan_response("alice", old_reply)[0]
    assert subagent_runtime._PROTOCOL.gate("alice") == gate
    assert "Plan requested" in subagent_runtime.run_request_plan("alice", "new work").content
    assert subagent_runtime._PROTOCOL.gate("alice") == "required"
    if old_reply:
        assert not subagent_runtime.apply_plan_response("alice", old_reply)[0]
    if gate == "pending":
        assert "not the current plan" in subagent_runtime.run_review_plan(old_id, True).content
    assert "Blocked" in session.execute_tool(SimpleNamespace(
        name="write_file", input={"path": "blocked.txt", "content": "no"})).result.content
    new_id = request_id(subagent_runtime.submit_plan("alice", "new plan"))
    assert new_id != old_id
    assert "approved" in subagent_runtime.run_review_plan(new_id, True).content
    assert subagent_runtime.apply_plan_response("alice", subagent_runtime._BUS.read_inbox("alice")[-1])[0]
    assert session.execute_tool(SimpleNamespace(
        name="write_file", input={"path": "approved.txt", "content": "ok"})).result.content.startswith("Wrote")


def test_plan_reply_requires_matching_identity_type_and_decision(host_factory, monkeypatch):
    host = host_factory()
    session = spawn_paused(host, monkeypatch, require_plan=True)
    current = request_id(subagent_runtime.submit_plan("alice", "plan"))
    with pytest.raises(ValueError, match="already waiting for review"):
        subagent_runtime.submit_plan("alice", "duplicate")
    subagent_runtime.run_review_plan(current, True).content
    reply = subagent_runtime._BUS.read_inbox("alice")[-1]
    for field, value in [
        ("from", "bob"), ("to", "bob"), ("type", "shutdown_response"),
        ("approve", False), ("request_id", "req_stale"),
    ]:
        forged = {**reply, "metadata": dict(reply["metadata"])}
        if field in {"approve", "request_id"}:
            forged["metadata"][field] = value
        else:
            forged[field] = value
        assert not subagent_runtime.apply_plan_response("alice", forged)[0]
        assert "Blocked" in session.execute_tool(SimpleNamespace(
            name="write_file", input={"path": "blocked.txt", "content": "no"})).result.content
    assert subagent_runtime.apply_plan_response("alice", reply)[0]
    assert not subagent_runtime.apply_plan_response("alice", reply)[0]
    assert session.execute_tool(tool("write_file", path="approved.txt", content="ok")).result.status == "success"


def test_shutdown_routing_is_optional_and_cleanup_keeps_ack_for_lead(host_factory, monkeypatch):
    host = host_factory()
    session = spawn_paused(host, monkeypatch, require_plan=True)
    plan_id = request_id(subagent_runtime.submit_plan("alice", "pending plan"))
    agent_id = subagent_runtime._active_subagents["alice"].agent_id
    forged = {"from": "lead", "to": "alice", "type": "shutdown_request",
              "content": "stop", "metadata": {"request_id": "req_unknown"}}
    assert not subagent_runtime.apply_shutdown_request("alice", forged)[0]
    shutdown_id = request_id(subagent_runtime.run_request_shutdown(agent_id).content)
    subagent_runtime._BUS.send("bob", "lead", "wrong child", "shutdown_response",
                               {"request_id": shutdown_id, "approve": True})
    subagent_runtime.consume_lead_inbox()
    assert subagent_runtime._PROTOCOL.request(shutdown_id).status == "pending"
    request = subagent_runtime._BUS.read_inbox("alice")[-1]
    assert subagent_runtime.apply_shutdown_request("alice", request) == (True, shutdown_id)
    assert not subagent_runtime.apply_shutdown_request("alice", request)[0]
    subagent_runtime._BUS.send("alice", "lead", "ack", "shutdown_response",
                  {"request_id": shutdown_id, "approve": True})
    delivered = subagent_runtime.consume_lead_inbox(route_protocol=False)
    assert any(message["type"] == "shutdown_response" for message in delivered)
    assert subagent_runtime._PROTOCOL.request(shutdown_id).status == "pending"
    subagent_runtime._BUS.send("alice", "lead", "ack after cleanup", "shutdown_response",
                  {"request_id": shutdown_id, "approve": True})
    record = subagent_runtime._active_subagents["alice"]
    subagent_runtime._cleanup(record)
    assert subagent_runtime._PROTOCOL.request(plan_id) is None
    assert subagent_runtime._PROTOCOL.request(shutdown_id) is None
    assert subagent_runtime._PROTOCOL.empty()
    assert subagent_runtime.consume_lead_inbox()[0]["content"] == "ack after cleanup"
    with pytest.raises(ValueError, match="not active"):
        subagent_runtime.submit_plan("alice", "late plan")
    assert "not active" in subagent_runtime.run_request_shutdown("alice").content


@pytest.mark.parametrize("kind,order", [("plan", "request_first"), ("shutdown", "cleanup_first")])
def test_request_creation_and_cleanup_are_atomic(host_factory, monkeypatch, kind, order):
    host = host_factory()
    real_thread = threading.Thread
    spawn_paused(host, monkeypatch, require_plan=True)
    record = subagent_runtime._active_subagents["alice"]
    reached = threading.Event()
    release = threading.Event()
    results = []
    errors = []
    send = subagent_runtime._BUS.send

    def delayed_send(*args, **kwargs):
        reached.set()
        assert release.wait(3)
        return send(*args, **kwargs)

    def request():
        try:
            if order == "cleanup_first":
                reached.set()
            results.append(subagent_runtime.submit_plan("alice", "plan") if kind == "plan"
                           else subagent_runtime.run_request_shutdown("alice").content)
        except Exception as exc:
            errors.append(exc)

    if order == "request_first":
        monkeypatch.setattr(subagent_runtime._BUS, "send", delayed_send)
        worker = real_thread(target=request)
        worker.start()
        try:
            assert reached.wait(3)
            subagent_runtime._cleanup(record)
        finally:
            release.set()
    else:
        # Follow the production lock order while blocking the other caller.
        with tasks.task_lock, subagent_runtime._subagent_lock:
            worker = real_thread(target=request)
            worker.start()
            assert reached.wait(3)
            subagent_runtime._cleanup(record)
    worker.join(3)
    assert not worker.is_alive()
    assert subagent_runtime._PROTOCOL.empty() and "alice" not in subagent_runtime._active_subagents
    if order == "cleanup_first":
        if kind == "plan":
            assert len(errors) == 1 and isinstance(errors[0], ValueError)
            assert "not active" in str(errors[0])
        else:
            assert not errors and "not active" in results[0]
    else:
        assert not errors
        assert "submitted" in results[0] if kind == "plan" else "Shutdown requested" in results[0]
        assert "not found" in subagent_runtime.run_review_plan(request_id(results[0]), True).content


def test_invalid_tools_do_not_claim_or_request(host_factory):
    for tools in ['bash', ['unknown'], ['spawn_subagent'], ['send_message'], ['claim_task'], [None]]:
        host = host_factory()
        task = tasks.create_task("Untouched task")
        client = S(messages=S(create=lambda **kw: pytest.fail("must not request")))
        result = subagent_runtime.spawn_subagent("child", "Inspect", "once",
                                             description="Inspect", tools=tools, client=client)
        assert result["status"] == "failed"
        assert result["usage"] == {"tokens": 0, "model_calls": 0,
                                   "input_tokens": 0, "output_tokens": 0}
        assert not subagent_runtime._active_subagents and not tasks.assignments
        assert tasks.load_task(task.id).status == "pending"


@pytest.mark.parametrize("budget", ["model", "tools"])
def test_once_default_budgets_preserve_partial_results_and_counts(
    host_factory, no_approval, monkeypatch, budget,
):
    host_factory()
    requests = []
    histories = []
    executed = []
    (core.WORKDIR / "evidence.txt").write_text("evidence", encoding="utf-8")
    monkeypatch.setitem(hooks.HOOKS, "PostToolUse", [
        lambda block, output: executed.append(block.id)])

    def create(**kwargs):
        requests.append(kwargs)
        histories.append(subagent_runtime._active_subagents["bounded"].messages)
        count = 43 if budget == "tools" else 1
        return S(stop_reason="tool_use", usage=S(input_tokens=10, output_tokens=3),
                 content=[S(type="text", text="Partial audit finding"), *[
                     S(type="tool_use", id=f"read-{len(requests)}-{index}",
                       name="read_file", input={"path": "evidence.txt"})
                     for index in range(count)]])

    result = subagent_runtime.spawn_subagent(
        "bounded", "Inspect evidence", "once", description="Inspect source evidence",
        tools=["read_file"], client=S(messages=S(create=create)))

    assert result["status"] == "budget_exhausted"
    assert result["text"] == "Partial audit finding"
    assert result["rounds"] == (1 if budget == "tools" else 12)
    assert result["tool_calls"] == (40 if budget == "tools" else 12)
    assert len(requests) == result["rounds"]
    assert len(executed) == result["tool_calls"]
    assert result["usage"]["input_tokens"] == 10 * len(requests)
    assert result["usage"]["output_tokens"] == 3 * len(requests)
    assert subagent_runtime.get_subagent_output(result["agent_id"], block=False) == result
    if budget == "tools":
        paired = histories[-1][-1]["content"]
        assert len(paired) == 43
        assert all(item.get("is_error") for item in paired[40:])
        assert all("Stopped" in item["content"] for item in paired[40:])
    assert not subagent_runtime._active_subagents


def test_empty_tools_never_execute_hallucinated_call(host_factory):
    host = host_factory()
    calls = []
    block = S(type="tool_use", name="write_file", id="write",
              input={"path": "forbidden.txt", "content": "no"})
    def create(**kw):
        calls.append(kw)
        return (response(None, tools=[block], usage=(5, 2)) if len(calls) == 1
                else response("done", usage=(5, 2)))
    result = subagent_runtime.spawn_subagent("child", "Read text", "once", tools=[],
                                         description="Read text", client=S(messages=S(create=create)))
    assert not calls[0].get("tools")
    assert not (core.WORKDIR / "forbidden.txt").exists()
    assert result["usage"] == {"tokens": 14, "model_calls": 2,
                               "input_tokens": 10, "output_tokens": 4}
    assert result["status"] == "completed"


def test_usage_includes_summary_and_failed_request(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setattr(core, "COMPACT_TRIGGER", 30)
    monkeypatch.setattr(core, "COMPACT_TARGET", 20)
    requests = []
    def create(**kw):
        requests.append(kw)
        if "tools" not in kw:
            return response("summary", usage=(7, 2))
        raise RuntimeError("execution unavailable")
    result = subagent_runtime.spawn_subagent("child", "Long instruction " * 20, "once",
                                         description="Inspect long input",
                                         client=S(messages=S(create=create)), model="injected",
                                         max_tokens=123)
    assert result["status"] == "failed"
    assert result["usage"] == {"tokens": 9, "model_calls": 2,
                               "input_tokens": 7, "output_tokens": 2}
    assert requests[0]["max_tokens"] == core.COMPACTION_MAX_TOKENS
    assert all(r["model"] == "injected" for r in requests)
    assert not subagent_runtime._active_subagents
