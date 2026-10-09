"""Offline integration through the real serialized driver and host loop."""

from __future__ import annotations

import threading
import time
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def response(text="Done", *, tools=(), stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text), *tools],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=3))


def tool(tool_name, call_id="call", **arguments):
    return SimpleNamespace(type="tool_use", name=tool_name, id=call_id, input=arguments)


def until(runtime, event_type, *, submission_id=None, timeout=5):
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = runtime.channel.next_event(timeout=0.05)
        if event is not None:
            events.append(event)
            if event.type == event_type and (submission_id is None or event.payload.get("submission_id") == submission_id):
                return event, events
    raise AssertionError(f"Missing {event_type}: {events}")


@pytest.fixture
def runtime_factory(host_factory, monkeypatch):
    from agent_harness import interactive_runtime
    host = host_factory(enable_memory=True)
    remember = host.remember_after_turn
    monkeypatch.setattr(interactive_runtime, "host", host)
    monkeypatch.setattr(host.core, "ASYNC_EVENT_POLL_SECONDS", 0.01)
    runtimes = []

    def create(responses=None, client=None, enable_memory=False):
        monkeypatch.setattr(host, "remember_after_turn", remember if enable_memory else lambda messages: None)
        if client is None:
            values = iter(responses or [response()])
            client = lambda **kwargs: next(values)
        monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=client)))
        runtime = interactive_runtime.InteractiveRuntime()
        runtimes.append(runtime)
        runtime.start()
        until(runtime, "state_snapshot")
        return runtime, host

    yield create
    for runtime in runtimes:
        runtime.close()
        runtime._thread.join(timeout=5)
        assert not runtime._thread.is_alive()
    assert host.scheduler._runtime_thread is None
    assert not host.subagent.active_subagents


def test_serial_submission_history_events_and_usage(runtime_factory):
    started, release = threading.Event(), threading.Event()
    def create(**kwargs):
        started.set()
        assert release.wait(3)
        return response("**你好**")
    runtime, host = runtime_factory(client=create)
    submission = runtime.submit("hello")
    assert started.wait(2)
    assert runtime.submit("second").status == "busy"
    release.set()
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert finished.payload["saved"]
    assert [event.payload["text"] for event in events if event.type == "assistant_message"] == ["**你好**"]
    snapshot = [event.payload for event in events if event.type == "state_snapshot"][-1]
    assert snapshot["lead_main_model_usage"] == {"input_tokens": 10, "output_tokens": 3, "model_calls": 1}
    saved = runtime.session.store.load(runtime.session.session_id)
    assert saved["messages"][0]["content"] == "hello"
    assert saved["messages"][-1]["content"][0]["text"] == "**你好**"
    assert not runtime.request_stop(finished.turn_id)
    assert runtime.submit("/goal").status == "accepted"


@pytest.mark.parametrize("decision,expected", [("approve", "hello"), ("deny", "Permission denied")])
def test_approval_routes_to_matching_tool(runtime_factory, decision, expected):
    runtime, host = runtime_factory([response("Before", tools=[tool("bash", command="printf hello")], stop="tool_use"), response("After")])
    submission = runtime.submit("run")
    request = runtime.channel.next_approval(timeout=3)
    assert request and request.tool_call_id == "call" and request.source == "lead"
    assert runtime.channel.respond(request.request_id, request.generation, decision)
    _, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    results = [event.payload["content"] for event in events if event.type == "tool_result"]
    assert expected in results[0]
    assert [event.payload["text"] for event in events if event.type == "assistant_message"] == ["Before", "After"]


def test_stop_waiting_approval_completes_tools_and_pauses_background(runtime_factory):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="touch unwanted"), tool("write_file", "write", path="unwanted", content="x")], stop="tool_use"), response("Next")])
    submission = runtime.submit("run")
    request = runtime.channel.next_approval(timeout=3)
    assert request is not None
    assert runtime.request_stop(request.turn_id)
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert not (host.core.WORKDIR / "unwanted").exists()
    assert len([event for event in events if event.type == "tool_result"]) == 2
    host.subagent.BUS.send("child", "lead", "Waiting result")
    assert runtime._background_paused
    assert runtime.submit("continue").status == "accepted"
    assert not runtime._background_paused


def test_close_waiting_approval_is_nonblocking_and_terminal(runtime_factory):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="touch unwanted")], stop="tool_use")])
    runtime.submit("run")
    request = runtime.channel.next_approval(timeout=3)
    assert request
    runtime.close()
    runtime.close()
    assert runtime.submit("new").status == "closed"
    closed, events = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    assert len([event for event in events if event.type == "turn_finished"]) == 1
    assert not (host.core.WORKDIR / "unwanted").exists()


@pytest.mark.parametrize("mode", ["failure", "truncated"])
def test_host_outcomes_are_structured(runtime_factory, mode):
    def create(**kwargs):
        if mode == "failure":
            raise RuntimeError("offline failure")
        return response("partial", stop="max_tokens")
    runtime, host = runtime_factory(client=create)
    submission = runtime.submit("run")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == ("failed" if mode == "failure" else "truncated")


def test_commands_resume_generation_and_save_failure(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    target["messages"] = [{"role": "user", "content": "old"}]
    runtime.session.store.save(target)
    submission = runtime.submit("/goal")
    command, _ = until(runtime, "command_result", submission_id=submission.submission_id)
    assert command.payload["status"] == "completed"
    previous = runtime.session.session_id
    save = runtime.session.store.save
    def fail(snapshot):
        raise OSError("disk unavailable")
    monkeypatch.setattr(runtime.session.store, "save", fail)
    submission = runtime.submit("/resume " + target["id"])
    command, events = until(runtime, "command_result", submission_id=submission.submission_id)
    assert command.payload["status"] == "failed"
    assert runtime.session.session_id == previous
    assert any(event.type == "session_save_error" for event in events)
    monkeypatch.setattr(runtime.session.store, "save", save)
    submission = runtime.submit("/resume " + target["id"])
    command, events = until(runtime, "command_result", submission_id=submission.submission_id)
    assert command.generation == 2
    assert runtime.session.history == target["messages"]
    assert len([event for event in events if event.type == "session_changed"]) == 1


def test_save_failure_does_not_replace_success_outcome(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    def fail(snapshot):
        raise OSError("disk unavailable")
    monkeypatch.setattr(runtime.session.store, "save", fail)
    submission = runtime.submit("run")
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert not finished.payload["saved"]
    assert any(event.type == "session_save_error" for event in events)
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert not closed.payload["saved"]


def test_goal_command_correlates_followup_turn(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    monkeypatch.setattr(host, "evaluate_goal", lambda messages: None)
    submission = runtime.submit("/goal write report")
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    command = next(event for event in events if event.type == "command_result")
    assert command.payload["turn_id"] == finished.turn_id
    assert runtime.session.active_request == "write report"


def test_cli_owns_all_reads_and_waits_for_goal_followup(runtime_factory, monkeypatch):
    from agent_harness.cli import CLIAdapter
    runtime, host = runtime_factory([response(tools=[tool("bash", command="printf hello")], stop="tool_use"), response("After")])
    monkeypatch.setattr(host, "evaluate_goal", lambda messages: None)
    answers = iter(["/goal do it", "yes", "q"])
    reads, output = [], []
    def reader(prompt=None):
        reads.append((threading.get_ident(), prompt))
        return next(answers)
    CLIAdapter(runtime, reader, output.append).run()
    assert {ident for ident, _ in reads} == {threading.get_ident()}
    assert reads[1][1] == "Allow? [y/N] "
    assert output.count("After") == 1
    assert "hello" in str(output)


def test_stop_after_model_does_not_start_goal_or_memory(runtime_factory, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def create(**kwargs):
        started.set()
        assert release.wait(3)
        return response()
    runtime, host = runtime_factory(client=create)
    calls = []
    monkeypatch.setattr(host, "evaluate_goal", lambda messages: calls.append("goal"))
    monkeypatch.setattr(host, "remember_after_turn", lambda messages: calls.append("memory"))
    submission = runtime.submit("run")
    assert started.wait(2)
    started_event, _ = until(runtime, "turn_started")
    assert runtime.request_stop(started_event.turn_id)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert not calls


@pytest.mark.parametrize("operation", ["stop", "close"])
def test_stop_model_context_error_does_not_start_reactive_summary(runtime_factory, monkeypatch, operation):
    started, release = threading.Event(), threading.Event()
    calls, summaries = [], []
    def create(**kwargs):
        calls.append(True)
        started.set()
        assert release.wait(3)
        raise RuntimeError("context_length_exceeded")
    runtime, host = runtime_factory(client=create)
    monkeypatch.setattr(host.compaction, "summarize_history",
                        lambda *args, **kwargs: summaries.append(True) or "summary")
    job = host.schedule_job("0 0 1 1 *", "deliver once", recurring=False)
    host.scheduler._enqueue_due_job(job)
    submission = runtime.submit("run")
    assert started.wait(2)
    turn, _ = until(runtime, "turn_started")
    if operation == "stop":
        assert runtime.request_stop(turn.turn_id)
    else:
        runtime.close()
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    # Stop must prevent new recovery work without concealing the model error.
    assert finished.payload["outcome"]["status"] == "failed"
    assert "context_length_exceeded" in finished.payload["outcome"]["reason"]
    assert calls == [True] and summaries == []
    assert host.scheduler.cron_queue == [job]
    assert host.scheduler.scheduled_jobs[job.id].pending_delivery
    if operation == "close":
        until(runtime, "runtime_closed")


@pytest.mark.parametrize("boundary", ["goal", "memory"])
def test_stop_during_closeout_blocks_subsequent_work(runtime_factory, monkeypatch, boundary):
    started, release = threading.Event(), threading.Event()
    runtime, host = runtime_factory()
    calls = []
    def block(messages):
        calls.append(boundary)
        started.set()
        assert release.wait(3)
        return SimpleNamespace(action="block", reason="More work") if boundary == "goal" else None
    monkeypatch.setattr(host, "evaluate_goal", block if boundary == "goal" else lambda messages: None)
    monkeypatch.setattr(host, "remember_after_turn", block if boundary == "memory" else lambda messages: calls.append("memory"))
    submission = runtime.submit("run")
    assert started.wait(2)
    started_event, _ = until(runtime, "turn_started")
    assert runtime.request_stop(started_event.turn_id)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert calls == [boundary]
    assert "[Goal still active]" not in str(runtime.session.history)


def test_stop_during_real_memory_extraction_preserves_writes_without_consolidation(runtime_factory, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return response("Preference noted")
        started.set()
        assert release.wait(3)
        return response('[{"name":"language","type":"user","scope":"persistent",'
                        '"description":"Preferred language","body":"Use Chinese in future sessions."}]')
    runtime, host = runtime_factory(client=create, enable_memory=True)
    consolidated = []
    monkeypatch.setattr(host.memory_runtime, "consolidate_memories", lambda: consolidated.append(True))
    submission = runtime.submit("Please remember to use Chinese in future sessions")
    assert started.wait(2)
    started_event, _ = until(runtime, "turn_started")
    assert runtime.request_stop(started_event.turn_id)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert len(calls) == 2
    assert not consolidated
    assert host.memory_runtime.list_memory_files()[0]["name"] == "language"


@pytest.mark.parametrize("boundary", ["before_model", "after_response"])
def test_stop_cron_delivery_boundary(runtime_factory, monkeypatch, boundary):
    started, release = threading.Event(), threading.Event()
    def block():
        started.set()
        assert release.wait(3)
    def create(**kwargs):
        if boundary == "after_response":
            block()
        return response()
    runtime, host = runtime_factory(client=create)
    if boundary == "before_model":
        prepare = host.compaction.prepare_history
        def prepare_blocked(*args, **kwargs):
            block()
            return prepare(*args, **kwargs)
        monkeypatch.setattr(host.compaction, "prepare_history", prepare_blocked)
    job = host.schedule_job("0 0 1 1 *", "deliver once", recurring=False)
    host.scheduler._enqueue_due_job(job)
    submission = runtime.submit("run")
    assert started.wait(2)
    started_event, _ = until(runtime, "turn_started")
    assert runtime.request_stop(started_event.turn_id)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert runtime._background_paused
    if boundary == "before_model":
        assert host.scheduler.cron_queue == [job]
        assert host.scheduler.scheduled_jobs[job.id].pending_delivery
    else:
        assert not host.scheduler.cron_queue
        assert job.id not in host.scheduler.scheduled_jobs


def test_background_delivery_runs_with_noninteractive_permissions(runtime_factory):
    runtime, host = runtime_factory([response("initial"), response(tools=[tool("bash", command="touch unwanted")], stop="tool_use"), response("delivered")])
    submission = runtime.submit("run")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    host.subagent.BUS.send("child", "lead", "Background result")
    finished, events = until(runtime, "turn_finished")
    assert finished.payload["submission_id"] is None
    assert finished.payload["outcome"]["status"] == "completed"
    assert runtime.channel.next_approval() is None
    assert not (host.core.WORKDIR / "unwanted").exists()
    assert "Background result" in str(runtime.session.history)
    assert "execution context" in str([event.payload for event in events if event.type == "tool_result"])


def test_frontend_once_inherits_approval_and_workflow_does_not(runtime_factory):
    from agent_harness import workflow_runtime
    child_call = tool("spawn_subagent", name="helper", prompt="execute", mode="once")
    runtime, host = runtime_factory([response(tools=[child_call], stop="tool_use"),
                                     response(tools=[tool("bash", command="printf child")], stop="tool_use"),
                                     response("child done"), response("lead done")])
    submission = runtime.submit("run")
    request = runtime.channel.next_approval(timeout=3)
    assert request and request.source == "helper"
    assert runtime.channel.respond(request.request_id, request.generation, "approve")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"

    from agent_harness.interaction import ExecutionContext, execution_context
    values = iter([response(tools=[tool("bash", command="touch unwanted")], stop="tool_use"), response("workflow done")])
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: next(values)))
    context = ExecutionContext(runtime.channel, runtime.session.session_id, 1, "frontend", origin="frontend")
    with execution_context(context):
        result = workflow_runtime.SubagentRunner(client=client).run("work")
    assert result.value == "workflow done"
    assert runtime.channel.next_approval() is None
    assert not (host.core.WORKDIR / "unwanted").exists()


def test_close_waits_for_owned_persistent_thread(runtime_factory):
    child_call = tool("spawn_subagent", name="helper", prompt="work", mode="persistent")
    def create(**kwargs):
        if "You are 'helper'" in str(kwargs.get("system")):
            return response("child done")
        if len(kwargs["messages"]) == 1:
            return response(tools=[child_call], stop="tool_use")
        return response("lead done")
    runtime, host = runtime_factory(client=create)
    submission = runtime.submit("run")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    assert not host.subagent.active_subagents
    assert not host.subagent._persistent_threads


@pytest.fixture
def session_worktree(runtime_factory):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    root = host.core.WORKDIR
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=Test", "-c",
                               "user.email=test@example.invalid", *args], cwd=root,
                              check=True, capture_output=True, text=True)
    git("init", "-q")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "initial")
    submission = runtime.submit("session A")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    with host.agent_lock:
        task = host.create_task("session A task")
        assert host.create_worktree("feature", task.id).startswith("Worktree")
        assert host.claim_task(task.id).startswith("Claimed")
        host.run_todo_write([{"content": "验证中文目录", "status": "pending"}])
    return runtime, host, task, root / ".worktrees" / "feature"


def resume_command(runtime, session_id):
    submission = runtime.submit(f"/resume {session_id}")
    assert submission.status == "accepted"
    return until(runtime, "command_result", submission_id=submission.submission_id)


def test_resume_isolates_task_directory_and_todos(session_worktree):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    saved = runtime.session.store.load(a)
    assert saved["task_id"] == task.id
    assert saved["worktree"] == "feature"
    assert saved["todos"][0]["content"] == "验证中文目录"
    assert host.load_task(task.id).session_id == a
    b = runtime.session.store.create()
    b["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(b)
    result, _ = resume_command(runtime, b["id"])
    assert result.payload["status"] == "completed", result.payload
    assert host.assignment_cwd("agent") == host.core.WORKDIR
    assert not host.tasks.CURRENT_TODOS
    with host.agent_lock:
        assert "another session" in host.complete_task(task.id)
        assert "cannot claim" in host.claim_task(task.id)
        other = host.create_task("session B task")
        assert host.claim_task(other.id).startswith("Claimed")
    result, events = resume_command(runtime, a)
    assert result.payload["status"] == "completed"
    assert host.assignment_cwd("agent") == checkout
    assert host.tasks.CURRENT_TODOS == saved["todos"]
    assert host.load_task(other.id).status == "in_progress"
    display = [e for e in events if e.type == "state_snapshot"][-1]
    assert display.payload["todos"] == saved["todos"]
    assert host.run_agent_write("resumed.txt", "continued").startswith("Wrote")
    assert (checkout / "resumed.txt").read_text() == "continued"
    assert not (host.core.WORKDIR / "resumed.txt").exists()


@pytest.mark.parametrize("change", ["missing_task", "owner", "session", "binding", "directory", "startup"])
def test_resume_rejects_invalid_association_without_changing_current(session_worktree, change):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    b = runtime.session.store.create()
    b["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(b)
    switched, _ = resume_command(runtime, b["id"])
    assert switched.payload["status"] == "completed", switched.payload
    if change == "missing_task":
        host.tasks._task_path(task.id).unlink()
    elif change == "directory":
        checkout.rename(checkout.with_name("moved"))
    elif change == "startup":
        saved = runtime.session.store.load(a)
        saved["workdir"] = str(host.core.WORKDIR / "other")
        runtime.session.store.save(saved)
    else:
        saved_task = host.load_task(task.id)
        if change == "owner":
            saved_task.owner = "other"
        elif change == "session":
            saved_task.session_id = "20261009-000000-deadbeef"
        else:
            saved_task.worktree = None
        host.save_task(saved_task)
    before = list(runtime.session.history)
    result, _ = resume_command(runtime, a)
    assert result.payload["status"] == "failed"
    assert runtime.session.session_id == b["id"]
    assert runtime.session.history == before
    assert host.tasks.lead_session_id == b["id"]
    assert host.assignment_cwd("agent") == host.core.WORKDIR


@pytest.mark.parametrize("state", ["in_progress", "completed", "interrupted_completion"])
def test_restart_restores_task_and_retained_directory(session_worktree, monkeypatch, state):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    original_save = runtime.session.store.save
    def fail(snapshot):
        raise OSError("disk full")
    with host.agent_lock:
        if state == "interrupted_completion":
            monkeypatch.setattr(runtime.session.store, "save", fail)
        if state != "in_progress":
            assert host.complete_task(task.id).startswith("Completed")
            if state == "interrupted_completion":
                assert runtime.session.store.load(a)["task_id"] == task.id
            host.release_completed_assignment("agent")
    # A failed final save leaves the pre-completion association on disk.
    runtime.close()
    until(runtime, "runtime_closed")
    runtime._thread.join(timeout=5)
    monkeypatch.setattr(runtime.session.store, "save", original_save)
    from agent_harness.interactive_runtime import InteractiveRuntime
    fresh = InteractiveRuntime()
    try:
        fresh.start()
        until(fresh, "state_snapshot")
        assert host.assignment_cwd("agent") == host.core.WORKDIR
        assert not host.tasks.CURRENT_TODOS
        submission = fresh.submit("new session after restart")
        until(fresh, "turn_finished", submission_id=submission.submission_id)
        with host.agent_lock:
            other = host.create_task("new session task")
            assert host.claim_task(other.id).startswith("Claimed")
            assert host.assignment_cwd("agent") == host.core.WORKDIR
        result, _ = resume_command(fresh, a)
        assert result.payload["status"] == "completed"
        assert ("agent" in host.tasks.assignments) == (state == "in_progress")
        assert host.assignment_cwd("agent") == checkout
        assert fresh.session.store.load(a)["task_id"] == (task.id if state == "in_progress" else None)
        assert host.load_task(task.id).status == ("in_progress" if state == "in_progress" else "completed")
        assert host.tasks.CURRENT_TODOS[0]["content"] == "验证中文目录"
    finally:
        fresh.close()
        until(fresh, "runtime_closed")
        fresh._thread.join(timeout=5)


def test_worktree_removal_clears_current_directory_and_rejects_stale_context(session_worktree):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    stale = runtime.session.store.load(a)
    stale["id"] = runtime.session.store.create()["id"]
    stale["task_id"] = None
    runtime.session.store.save(stale)
    with host.agent_lock:
        assert host.complete_task(task.id).startswith("Completed")
        host.release_completed_assignment("agent")
        assert host.assignment_cwd("agent") == checkout
        assert host.remove_worktree("feature").startswith("Worktree")
        assert host.assignment_cwd("agent") == host.core.WORKDIR
    saved = runtime.session.store.load(a)
    assert saved["worktree"] is None
    assert saved["task_id"] is None
    result, _ = resume_command(runtime, stale["id"])
    assert result.payload["status"] == "failed"
    assert runtime.session.session_id == a


def test_save_failure_keeps_all_current_associations(session_worktree, monkeypatch):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    target["todos"] = [{"content": "different plan", "status": "completed"}]
    runtime.session.store.save(target)
    before_history = list(runtime.session.history)
    before_goal = host.GOAL
    before_assignment = dict(host.tasks.assignments["agent"])
    original_save = runtime.session.store.save
    def fail(snapshot):
        raise OSError("disk full")
    monkeypatch.setattr(runtime.session.store, "save", fail)
    result, _ = resume_command(runtime, target["id"])
    assert result.payload["status"] == "failed"
    assert runtime.session.session_id == host.tasks.lead_session_id == a
    assert runtime.session.history == before_history
    assert host.GOAL is before_goal
    assert host.tasks.assignments["agent"] == before_assignment
    assert host.assignment_cwd("agent") == checkout
    assert host.tasks.CURRENT_TODOS == [{"content": "验证中文目录", "status": "pending"}]
    assert runtime._generation == 1
    monkeypatch.setattr(runtime.session.store, "save", original_save)


def test_model_claim_and_todo_are_saved_before_next_request(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    task = host.create_task("model task")
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return response(tools=[tool("claim_task", "claim", task_id=task.id),
                                   tool("todo_write", "todo", todos=[{"content": "verify", "status": "in_progress"}])],
                            stop="tool_use")
        saved = runtime.session.store.load(runtime.session.session_id)
        assert saved["task_id"] == task.id
        assert saved["todos"] == [{"content": "verify", "status": "in_progress"}]
        assert host.load_task(task.id).session_id == runtime.session.session_id
        return response("Task retained")
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    submission = runtime.submit("claim and track task")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert len(calls) == 2


def test_restart_from_other_directory_finds_and_restores_session(session_worktree, monkeypatch):
    runtime, host, task, checkout = session_worktree
    session_id = runtime.session.session_id
    original_root = host.core.WORKDIR
    runtime.close()
    until(runtime, "runtime_closed")
    runtime._thread.join(timeout=5)

    other_root = original_root / "其他启动目录"
    other_root.mkdir()
    monkeypatch.chdir(other_root)
    host.core.WORKDIR = other_root
    from agent_harness.interactive_runtime import InteractiveRuntime
    fresh = InteractiveRuntime()
    try:
        fresh.start()
        until(fresh, "state_snapshot")
        entries, errors = fresh.session.store.list_sessions()
        assert not errors
        assert session_id in [entry["id"] for entry in entries]
        result, _ = resume_command(fresh, session_id)
        assert result.payload["status"] == "completed", result.payload
        assert Path.cwd() == host.core.WORKDIR == original_root
        assert fresh.session.store.workdir == original_root
        assert host.assignment_cwd("agent") == checkout
        assert host.load_task(task.id).session_id == session_id
        assert host.tasks.CURRENT_TODOS[0]["content"] == "验证中文目录"
        submission = fresh.submit("continue after moving")
        until(fresh, "turn_finished", submission_id=submission.submission_id)
        assert fresh.session.store.load(session_id)["workdir"] == str(original_root)
    finally:
        fresh.close()
        until(fresh, "runtime_closed")
        fresh._thread.join(timeout=5)


def test_switch_directory_reloads_cron_and_keeps_each_session_root(runtime_factory):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    original_root = host.core.WORKDIR
    submission = runtime.submit("original session")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    original_id = runtime.session.session_id
    original_job = host.schedule_job("0 0 1 1 *", "original", durable=True)
    transient_job = host.schedule_job("0 0 1 1 *", "transient", durable=False)

    other_root = original_root / "other"
    other_root.mkdir()
    other = runtime.session.store.create()
    other.update(workdir=str(other_root), goal=host.GOAL.snapshot())
    runtime.session.store.save(other)
    result, _ = resume_command(runtime, other["id"])
    assert result.payload["status"] == "completed", result.payload
    assert Path.cwd() == host.core.WORKDIR == other_root
    assert not host.scheduler.scheduled_jobs
    assert host.assignment_cwd("agent") == other_root
    assert host.run_agent_write("here.txt", "other").startswith("Wrote")
    assert (other_root / "here.txt").read_text() == "other"
    other_job = host.schedule_job("0 0 1 1 *", "other", durable=True)

    result, _ = resume_command(runtime, original_id)
    assert result.payload["status"] == "completed", result.payload
    assert Path.cwd() == host.core.WORKDIR == original_root
    assert set(host.scheduler.scheduled_jobs) == {original_job.id}
    assert transient_job.id not in host.scheduler.scheduled_jobs
    assert runtime.session.store.load(original_id)["workdir"] == str(original_root)
    assert runtime.session.store.load(other["id"])["workdir"] == str(other_root)
    result, _ = resume_command(runtime, other["id"])
    assert result.payload["status"] == "completed", result.payload
    assert set(host.scheduler.scheduled_jobs) == {other_job.id}


@pytest.mark.parametrize("failure", ["save", "chdir", "task", "queued_cron"])
def test_cross_directory_failure_preserves_current_context(runtime_factory, monkeypatch, failure):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    original_root = host.core.WORKDIR
    submission = runtime.submit("keep current")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    current_id = runtime.session.session_id
    other_root = original_root / "other"
    other_root.mkdir()
    target = runtime.session.store.create()
    target.update(workdir=str(other_root), goal=host.GOAL.snapshot())
    if failure == "task":
        target["task_id"] = "task_deadbeef"
    runtime.session.store.save(target)
    job = host.schedule_job("0 0 1 1 *", "keep scheduler", durable=True)
    if failure == "queued_cron":
        with host.scheduler.cron_lock:
            host.scheduler._enqueue_due_job(job)
        runtime._background_paused = True
    def fail(*args):
        raise OSError("unavailable")
    if failure == "save":
        monkeypatch.setattr(runtime.session.store, "save", fail)
    elif failure == "chdir":
        monkeypatch.setattr("agent_harness.interactive_runtime.os.chdir", fail)
    before_history = list(runtime.session.history)
    result, _ = resume_command(runtime, target["id"])
    assert result.payload["status"] == "failed", result.payload
    assert runtime.session.session_id == current_id
    assert runtime.session.history == before_history
    assert Path.cwd() == host.core.WORKDIR == runtime.session.store.workdir == original_root
    assert set(host.scheduler.scheduled_jobs) == {job.id}
    assert host.scheduler._runtime_thread.is_alive()
