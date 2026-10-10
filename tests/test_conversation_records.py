"""Durable conversation display records through the serialized runtime."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from test_interactive_runtime import (
    displayed_approval, response, runtime_factory, tool, until,
)


def stored(runtime, session_id=None):
    path = runtime.session.store.path(session_id or runtime.session.session_id)
    return json.loads(path.read_text(encoding="utf-8"))


def records(snapshot, event_type=None):
    values = [item for item in snapshot["events"] if item.get("kind") == "display"]
    return values if event_type is None else [item for item in values if item["type"] == event_type]


def wait_stored(runtime, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = stored(runtime)
        if predicate(value):
            return value
        time.sleep(0.02)
    raise AssertionError(f"Conversation record not saved: {stored(runtime)}")


def test_historical_running_operations_are_marked_interrupted(runtime_factory):
    runtime, _ = runtime_factory()
    runtime.session.activate(runtime.session.store.create())
    runtime.session.snapshot["events"] = [
        {"id": "running", "kind": "display", "type": "execution_status", "source": "lead",
         "turn_id": "turn", "payload": {"phase": "model", "name": "请求模型", "state": "running",
                                           "operation_id": "op", "model_call_id": "call"}},
        {"id": "child", "kind": "display", "type": "subagent_status", "source": "helper",
         "turn_id": "turn", "payload": {"name": "helper", "mode": "task", "status": "working"}},
        {"id": "background", "kind": "display", "type": "background_status", "source": "lead",
         "turn_id": "turn", "payload": {"task_id": "task", "status": "running"}},
    ]
    historical = runtime.session.display_records(historical=True)
    assert historical[0]["payload"]["state"] == "interrupted"
    assert historical[0]["payload"]["reason"] == "上次中断，结束状态未记录"
    assert historical[1]["payload"]["status"] == "interrupted"
    assert historical[2]["payload"]["status"] == "interrupted"


def test_historical_idle_subagent_is_not_marked_interrupted(runtime_factory):
    runtime, _ = runtime_factory()
    runtime.session.activate(runtime.session.store.create())
    runtime.session.snapshot["events"] = [
        {"id": "working", "kind": "display", "type": "subagent_status", "source": "helper",
         "turn_id": "turn", "payload": {"name": "helper", "mode": "persistent", "status": "working"}},
        {"id": "idle", "kind": "display", "type": "subagent_status", "source": "helper",
         "turn_id": "turn", "payload": {"name": "helper", "mode": "persistent", "status": "idle"}},
    ]
    historical = runtime.session.display_records(historical=True)
    assert historical[0]["payload"]["status"] == "working"
    assert historical[1]["payload"]["status"] == "idle"


def complete(runtime, text):
    submission = runtime.submit(text)
    assert submission.status == "accepted"
    return until(runtime, "turn_finished", submission_id=submission.submission_id)


def test_full_conversation_exit_resume_and_continue(runtime_factory):
    todos = [{"content": "Read command output", "status": "completed"}]
    runtime, host = runtime_factory([
        response("Plan", tools=[tool("todo_write", "todo", todos=todos)], stop="tool_use"),
        response("Execute", tools=[tool("bash", "shell", command="printf verified")], stop="tool_use"),
        response("Verified output"),
    ])
    submission = runtime.submit("Inspect the output")
    request, _ = displayed_approval(runtime)
    assert runtime.channel.respond(request.request_id, request.generation, "approve")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    session_id = runtime.session.session_id
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    runtime._thread.join(timeout=3)
    assert not runtime._thread.is_alive()
    original = records(stored(runtime))

    restored, host = runtime_factory([response("Continued answer")])
    submission = restored.submit(f"/resume {session_id}")
    changed, _ = until(restored, "session_changed")
    until(restored, "command_result", submission_id=submission.submission_id)
    historical = changed.payload["records"]
    assert [item["id"] for item in historical] == [item["id"] for item in original]
    assert host.tasks.CURRENT_TODOS == todos
    assert records(stored(restored), "approval_decided")[0]["payload"]["decision"] == "approve"
    assert all(item["payload"]["historical"] for item in historical)
    complete(restored, "Continue from that output")
    saved = restored.session.store.load(session_id)
    assert [item["id"] for item in records(saved)[:len(original)]] == [item["id"] for item in original]
    assert records(saved, "assistant_message")[-1]["payload"]["text"] == "Continued answer"
    assert len(records(saved, "approval_requested")) == 1
    assert len(records(saved, "tool_call")) == 2


def test_saved_records_match_live_order_and_contain_full_tool_payload(runtime_factory):
    runtime, host = runtime_factory([
        response("Before", tools=[tool("bash", command="printf 'Error: sample output'")], stop="tool_use"),
        response("After"),
    ])
    submission = runtime.submit("run command")
    request, before = displayed_approval(runtime)
    assert runtime.channel.respond(request.request_id, request.generation, "approve")
    finished, after = until(runtime, "turn_finished", submission_id=submission.submission_id)
    snapshot = stored(runtime)
    saved = records(snapshot)
    live = [event.payload["record"] for event in before + after if event.type == "display_record"]
    assert [item["id"] for item in saved] == [item["id"] for item in live]
    assert len({item["id"] for item in saved}) == len(saved)
    assert records(snapshot, "user_message")[0]["payload"]["text"] == "run command"
    assert [item["payload"]["text"] for item in records(snapshot, "assistant_message")] == ["Before", "After"]
    result = records(snapshot, "tool_result")[0]
    assert result["payload"]["content"] == "Error: sample output"
    assert result["payload"]["status"] == "returned"
    assert result["payload"]["tool_use_id"] == "call"
    tool_lifecycle = [item["payload"]["state"] for item in records(snapshot, "execution_status")
                      if item["payload"].get("phase") == "tool"]
    assert tool_lifecycle == ["running", "postprocessing", "completed"]
    summary = finished.payload["summary"]
    assert summary["tool_calls"] == summary["approvals"] == 1
    assert summary["elapsed_seconds"] >= 0
    assert summary["lead_usage"] == {"input_tokens": 20, "output_tokens": 6, "model_calls": 2}
    assert records(snapshot, "turn_finished")[0]["payload"]["summary"] == summary
    assert all(item.get("kind") in {"summary", "display"} for item in snapshot["events"])
    assert not any(item["type"] in {"session_saved", "state_snapshot", "log"} for item in saved)


@pytest.mark.parametrize("once", [False, True])
def test_approval_checkpoint_saves_request_and_background_while_waiting(runtime_factory, once):
    from agent_harness.runtime.interaction import ExecutionContext

    bash = response(tools=[tool("bash", command="printf approved")], stop="tool_use")
    values = ([response(tools=[tool("spawn_subagent", name="helper", prompt="run", mode="once")], stop="tool_use"),
               bash, response("child done"), response("lead done")] if once
              else [bash, response("lead done")])
    runtime, host = runtime_factory(values)
    submission = runtime.submit("execute")
    request, _ = displayed_approval(runtime)
    snapshot = stored(runtime)
    requested = records(snapshot, "approval_requested")
    assert len(requested) == 1
    assert requested[0]["payload"]["request_id"] == request.request_id
    assert requested[0]["source"] == ("helper" if once else "lead")
    assert records(snapshot, "tool_call")
    assert not records(snapshot, "tool_result")

    context = ExecutionContext(runtime.channel, runtime.session.session_id,
                               runtime._generation, request.turn_id,
                               source="observer", origin="persistent")
    worker = threading.Thread(target=lambda: context.publish(
        "subagent_status", name="observer", mode="persistent", status="completed"))
    worker.start()
    worker.join(timeout=2)
    wait_stored(runtime, lambda value: any(
        item["payload"].get("name") == "observer"
        for item in records(value, "subagent_status")))
    assert not records(stored(runtime), "approval_decided")
    assert runtime.channel.respond(request.request_id, request.generation, "deny")
    assert not runtime.channel.respond(request.request_id, request.generation, "approve")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    decided = records(stored(runtime), "approval_decided")
    assert len(decided) == 1
    assert decided[0]["payload"]["decision"] == "deny"
    results = records(stored(runtime), "tool_result")
    if not once:
        assert any(item["payload"]["status"] == "denied" for item in results)


def test_tool_request_and_approved_decision_are_saved_before_execution(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([
        response(tools=[tool("bash", command="printf checked")], stop="tool_use"), response("Done")])
    original = host.file_tools.run_bash
    observed = []

    def execute(*args, **kwargs):
        snapshot = stored(runtime)
        assert records(snapshot, "tool_call")[0]["payload"]["id"] == "call"
        assert records(snapshot, "approval_requested")
        assert records(snapshot, "approval_decided")[0]["payload"]["decision"] == "approve"
        assert not records(snapshot, "tool_result")
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(host.file_tools, "run_bash", execute)
    submission = runtime.submit("execute")
    request, _ = displayed_approval(runtime)
    assert runtime.channel.respond(request.request_id, request.generation, "approve")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert observed == [True]


def test_failed_save_retry_preserves_record_ids_and_does_not_repeat_records(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([response("First"), response("Second")])
    original = runtime.session.store.save
    attempts = []

    def fail(snapshot):
        attempts.append(copy.deepcopy(snapshot))
        raise OSError("offline disk failure")

    monkeypatch.setattr(runtime.session.store, "save", fail)
    complete(runtime, "first question")
    assert runtime.session.dirty
    before = records(runtime.session.snapshot)
    ids = [item["id"] for item in before]
    assert ids
    monkeypatch.setattr(runtime.session.store, "save", original)
    complete(runtime, "second question")
    saved = records(stored(runtime))
    assert [item["id"] for item in saved[:len(before)]] == ids
    assert len({item["id"] for item in saved}) == len(saved)
    assert len(records(stored(runtime), "user_message")) == 2
    assert not runtime.session.dirty
    assert attempts


def test_compaction_changes_model_messages_without_erasing_display_history(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([response("Original reply"), response("New reply")])
    complete(runtime, "original question")
    prior = records(stored(runtime))
    from agent_harness.context.compaction import CompactionResult
    applied = []

    def compact(messages, active_request, **kwargs):
        if not applied:
            messages[:] = [{"role": "user", "content": "Condensed model context"}]
            applied.append(True)
            return CompactionResult(True)
        return CompactionResult(False)

    monkeypatch.setattr(host.compaction, "prepare_history", compact)
    complete(runtime, "next question")
    snapshot = stored(runtime)
    assert "original question" not in str(snapshot["messages"])
    assert records(snapshot)[:len(prior)] == prior
    assert [item["payload"]["text"] for item in records(snapshot, "user_message")] == ["original question", "next question"]


def test_resume_replays_records_without_model_execution_or_duplicate_interruption(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([response("Earlier answer")])
    complete(runtime, "earlier question")
    snapshot = runtime.session.store.create()
    snapshot["goal"] = host.GOAL.snapshot()
    snapshot["messages"] = [
        {"role": "user", "content": "interrupted request"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "pending", "name": "bash",
                                             "input": {"command": "touch must-not-exist"}}]},
    ]
    snapshot["events"] = [
        {"id": "user", "at": snapshot["created_at"], "kind": "display", "type": "user_message",
         "source": "lead", "turn_id": "interrupted", "payload": {"text": "interrupted request"}},
        {"id": "tool", "at": snapshot["created_at"], "kind": "display", "type": "tool_call",
         "source": "lead", "turn_id": "interrupted", "payload": {"type": "tool_use", "id": "pending",
                                                                     "name": "bash", "input": {"command": "touch must-not-exist"}}},
        {"id": "approval", "at": snapshot["created_at"], "kind": "display", "type": "approval_requested",
         "source": "lead", "turn_id": "interrupted", "payload": {"request_id": "request", "tool_call_id": "pending",
                       "tool_name": "bash", "arguments": {"command": "touch must-not-exist"}}},
    ]
    runtime.session.store.save(snapshot)
    target = snapshot["id"]
    model_calls = []
    monkeypatch.setattr(host.core.client.messages, "create", lambda **kwargs: model_calls.append(kwargs))
    other = runtime.session.session_id

    def resume(session_id):
        submission = runtime.submit("/resume " + session_id)
        result, events = until(runtime, "command_result", submission_id=submission.submission_id)
        assert result.payload["status"] == "completed", result.payload
        return next(event for event in events if event.type == "session_changed")

    changed = resume(target)
    first = copy.deepcopy(changed.payload["records"])
    assert first
    assert runtime.channel.next_approval() is None
    assert not model_calls
    assert not (host.core.WORKDIR / "must-not-exist").exists()
    resume(other)
    changed = resume(target)
    historical = [item for item in changed.payload["records"] if item["type"] != "command_result"]
    assert historical == [item for item in first if item["type"] != "command_result"]
    assert not model_calls
    monkeypatch.setattr(host.core.client.messages, "create", lambda **kwargs: response("Continued safely"))
    complete(runtime, "continue from saved state")
    assert not (host.core.WORKDIR / "must-not-exist").exists()
    assert not any(item["payload"]["tool_use_id"] == "pending"
                   for item in records(stored(runtime), "tool_result"))
    model_results = [block for message in stored(runtime)["messages"]
                     if isinstance(message["content"], list) for block in message["content"]
                     if block["type"] == "tool_result" and block["tool_use_id"] == "pending"]
    assert len(model_results) == 1


@pytest.mark.parametrize("malformation", [
    "missing_display", "missing_reply", "missing_kind", "invalid_payload", "invalid_text", "duplicate_id",
    "invalid_summary", "invalid_outcome", "invalid_todo",
])
def test_invalid_current_context_is_rejected_without_modifying_files_or_active_session(runtime_factory, malformation):
    runtime, host = runtime_factory([response("Current answer")])
    complete(runtime, "current question")
    active_id = runtime.session.session_id
    active_history = copy.deepcopy(runtime.session.history)
    active_generation = runtime._generation
    snapshot = copy.deepcopy(stored(runtime))
    target = runtime.session.store.create()["id"]
    snapshot["id"] = target
    display = records(snapshot)
    if malformation == "missing_display":
        snapshot["events"] = [item for item in snapshot["events"] if item.get("kind") != "display"]
    elif malformation == "missing_reply":
        snapshot["events"] = [item for item in snapshot["events"] if item["type"] != "assistant_message"]
    elif malformation == "missing_kind":
        del display[0]["kind"]
    elif malformation == "invalid_payload":
        display[0]["payload"] = "invalid"
    elif malformation == "invalid_text":
        display[0]["payload"]["text"] = ["invalid"]
    elif malformation == "invalid_summary":
        records(snapshot, "turn_finished")[0]["payload"]["summary"]["tool_calls"] = "invalid"
    elif malformation == "invalid_outcome":
        records(snapshot, "turn_finished")[0]["payload"]["outcome"]["status"] = "invalid"
    elif malformation == "invalid_todo":
        snapshot["events"].append({
            "id": "invalid-todo", "at": snapshot["created_at"], "kind": "display",
            "type": "todo_updated", "source": "lead", "turn_id": "turn",
            "payload": {"todos": [{"content": "bad", "status": "unknown"}]},
        })
    else:
        display[1]["id"] = display[0]["id"]
    path = runtime.session.store.path(target)
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    original_bytes = path.read_bytes()
    submission = runtime.submit("/resume " + target)
    result, events = until(runtime, "command_result", submission_id=submission.submission_id)
    assert result.payload["status"] == "failed"
    assert path.read_bytes() == original_bytes
    assert runtime.session.session_id == active_id
    assert runtime.session.history == active_history
    assert runtime._generation == active_generation
    assert not any(event.type == "session_changed" for event in events)


def test_shutdown_saves_background_terminal_status_after_worker_join(runtime_factory, monkeypatch):
    from agent_harness.runtime.interaction import execution_context

    runtime, host = runtime_factory([response("Ready")])
    complete(runtime, "start session")
    started, release = threading.Event(), threading.Event()

    def process(command, cwd):
        started.set()
        assert release.wait(5)
        return "background result", 0

    monkeypatch.setattr(host.file_tools, "_run_bash_process", process)
    original_wait = host.scheduler.wait_for_background_tasks

    def wait_background():
        release.set()
        original_wait()

    monkeypatch.setattr(host.scheduler, "wait_for_background_tasks", wait_background)
    with host.agent_lock, execution_context(runtime._context(turn_id="background-final", origin="background")):
        task_id = host.scheduler.start_background_task(tool("bash", command="wait offline"), {})
    assert started.wait(2)
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    states = [item["payload"]["status"] for item in records(stored(runtime), "background_status")
              if item["payload"]["task_id"] == task_id]
    assert states == ["running", "completed"]


def test_backend_fact_queue_is_independent_of_frontend_delivery_and_detached():
    from agent_harness.runtime.interaction import ExecutionContext, InteractionChannel

    channel = InteractionChannel()
    context = ExecutionContext(channel, "session", 1, "turn", origin="frontend")
    arguments = {"id": "call", "name": "read_file", "input": {"path": "original"}}
    published = context.publish("tool_call", **arguments)
    arguments["input"]["path"] = "changed"
    frontend = channel.next_event()
    assert frontend.id == published.id
    assert channel.acknowledge_event(frontend.sequence)
    frontend.payload["input"]["path"] = "adapter mutation"
    facts = channel.drain_facts()
    assert len(facts) == 1
    assert facts[0].id == published.id
    assert facts[0].payload["input"]["path"] == "original"
    assert channel.drain_facts() == []


def test_todo_records_include_only_applied_updates(runtime_factory):
    todos = [{"content": "Check output", "status": "in_progress"}]
    runtime, host = runtime_factory([
        response(tools=[tool("todo_write", "valid", todos=todos),
                        tool("todo_write", "invalid", todos=[{"content": "bad", "status": "unknown"}])],
                 stop="tool_use"), response("Done")])
    complete(runtime, "track progress")
    updates = records(stored(runtime), "todo_updated")
    assert len(updates) == 1
    assert updates[0]["payload"]["todos"] == todos
    assert stored(runtime)["todos"] == todos


@pytest.mark.parametrize("action,decision", [("stop", "deny"), ("close", "closed")])
def test_stop_and_close_save_one_terminal_approval_decision(runtime_factory, action, decision):
    runtime, host = runtime_factory([
        response(tools=[tool("bash", command="touch must-not-exist")], stop="tool_use")])
    submission = runtime.submit("run")
    request, _ = displayed_approval(runtime)
    if action == "stop":
        assert runtime.request_stop(request.turn_id)
        until(runtime, "turn_finished", submission_id=submission.submission_id)
    else:
        runtime.close()
        until(runtime, "runtime_closed")
    terminal = records(stored(runtime), "approval_decided")
    assert len(terminal) == 1
    assert terminal[0]["payload"]["decision"] == decision
    assert not (host.core.WORKDIR / "must-not-exist").exists()


def test_synchronous_model_queues_background_facts_until_next_checkpoint(runtime_factory):
    from agent_harness.runtime.interaction import ExecutionContext

    started, release = threading.Event(), threading.Event()

    def model(**kwargs):
        started.set()
        assert release.wait(4)
        return response("Done")

    runtime, host = runtime_factory(client=model)
    submission = runtime.submit("wait for model")
    assert started.wait(2)
    context = ExecutionContext(runtime.channel, runtime.session.session_id,
                               runtime._generation, source="background", origin="background")
    context.publish("background_status", task_id="offline", status="completed")
    assert not records(stored(runtime), "background_status")
    release.set()
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert records(stored(runtime), "background_status")[0]["payload"]["task_id"] == "offline"


def test_failed_approval_checkpoint_does_not_retry_empty_polls(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([
        response(tools=[tool("bash", command="printf unused")], stop="tool_use"), response("Done")])
    attempts = []

    def fail(snapshot):
        attempts.append(time.monotonic())
        raise OSError("offline disk failure")

    monkeypatch.setattr(runtime.session.store, "save", fail)
    submission = runtime.submit("run")
    request, _ = displayed_approval(runtime)
    settled = len(attempts)
    time.sleep(0.35)
    assert len(attempts) == settled
    assert runtime.channel.respond(request.request_id, request.generation, "deny")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert len(attempts) > settled
def test_background_fact_is_visible_before_blocked_lead_checkpoint(runtime_factory):
    from agent_harness.runtime.interaction import ExecutionContext

    started, release = threading.Event(), threading.Event()
    def create(**kwargs):
        started.set()
        assert release.wait(10)
        return response("done")
    runtime, _ = runtime_factory(client=create)
    submission = runtime.submit("work")
    try:
        assert started.wait(2)
        context = ExecutionContext(runtime.channel, runtime.session.session_id,
                                   runtime._generation, source="helper", origin="persistent")
        fact = context.publish("background_status", task_id="running-job", status="completed")
        _, events = until(runtime, "background_status")
        displayed = runtime.channel.next_event(timeout=1)
        assert displayed is not None and displayed.type == "display_record"
        assert displayed.payload["record"]["id"] == fact.id
        assert displayed.payload["record"]["source"] == "helper"
        runtime.channel.acknowledge_event(displayed.sequence)
        assert not release.is_set()
    finally:
        release.set()
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert any(item["id"] == fact.id for item in records(stored(runtime)))

def test_first_tool_result_is_displayed_while_second_tool_is_blocked(runtime_factory, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtime, host = runtime_factory([
        response(tools=[tool("read_file", "first", path="first.txt"), tool("read_file", "second", path="second.txt")], stop="tool_use"),
        response("finished")])
    (host.core.WORKDIR / "first.txt").write_text("first result", encoding="utf-8")
    original = host.file_tools.run_read
    def read(path, **kwargs):
        if str(path).endswith("second.txt"):
            entered.set()
            assert release.wait(10)
        return original(path, **kwargs)
    monkeypatch.setattr(host.file_tools, "run_read", read)
    submission = runtime.submit("read two")
    try:
        assert entered.wait(3)
        _, events = until(runtime, "tool_result", timeout=2)
        assert events[-1].payload["tool_use_id"] == "first"
        assert "first result" in events[-1].payload["content"]
        saved = stored(runtime)
        assert len(records(saved, "tool_result")) == 1
        assert saved["messages"][-1]["role"] == "assistant"
    finally:
        release.set()
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert len(records(stored(runtime), "tool_result")) == 2
def test_resume_uses_saved_result_for_settled_tool_and_interrupts_only_missing_item(runtime_factory):
    entered, release = threading.Event(), threading.Event()
    runtime, host = runtime_factory([
        response(tools=[tool("read_file", "done", path="saved.txt"), tool("bash", "missing", command="printf pending")], stop="tool_use"),
        response("next")])
    (host.core.WORKDIR / "saved.txt").write_text("saved result", encoding="utf-8")
    submission = runtime.submit("read then ask")
    request, _ = displayed_approval(runtime)
    snapshot = stored(runtime)
    assert len(records(snapshot, "tool_result")) == 1
    assert snapshot["messages"][-1]["role"] == "assistant"
    runtime.channel.respond(request.request_id, request.generation, "deny")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    # A separate durable snapshot models a process lost while waiting for approval.
    target = runtime.session.store.create()
    snapshot.update(id=target["id"], created_at=target["created_at"])
    runtime.session.store.save(snapshot)
    submission = runtime.submit("/resume " + target["id"])
    until(runtime, "command_result", submission_id=submission.submission_id)
    complete(runtime, "continue")
    history = stored(runtime)["messages"]
    paired = [b for m in history if isinstance(m["content"], list) for b in m["content"] if b["type"] == "tool_result"]
    assert len(paired) == 2
    assert "saved result" in paired[0]["content"] and not paired[0].get("is_error")
    assert paired[1]["tool_use_id"] == "missing" and paired[1]["is_error"]
def test_stream_stop_discards_partial_body_without_retry_or_saved_message(runtime_factory):
    from contextlib import contextmanager
    from types import SimpleNamespace
    started, release = threading.Event(), threading.Event()
    attempts = []
    @contextmanager
    def stream(**kwargs):
        attempts.append(True)
        class Stream:
            def __iter__(self):
                yield SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text"))
                yield SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="partial"))
                started.set()
                assert release.wait(10)
                yield SimpleNamespace(type="content_block_stop", index=0)
                yield SimpleNamespace(type="message_stop")
            def get_final_message(self):
                return response("partial")
        yield Stream()
    runtime, _ = runtime_factory(stream=stream)
    submission = runtime.submit("stop during text")
    try:
        assert started.wait(2)
        delta, _ = until(runtime, "assistant_text_delta")
        assert runtime.request_stop(delta.turn_id)
    finally:
        release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert finished.payload["summary"]["lead_usage"]["model_calls"] == 1
    assert attempts == [True]
    assert not records(stored(runtime), "assistant_message")
    assert any(e["payload"]["state"] == "stopped" for e in records(stored(runtime), "execution_status"))


def test_truncated_retry_uses_distinct_identity_and_saves_only_accepted_text(runtime_factory):
    runtime, _ = runtime_factory([response("discard this", stop="max_tokens"), response("accepted")])
    finished, events = complete(runtime, "retry safely")
    calls = [r for r in records(stored(runtime), "execution_status") if r["payload"]["phase"] == "model"]
    assert len({r["payload"]["model_call_id"] for r in calls}) == 2
    assert any(r["payload"]["state"] == "discarded" for r in calls)
    assert [r["payload"]["text"] for r in records(stored(runtime), "assistant_message")] == ["accepted"]
