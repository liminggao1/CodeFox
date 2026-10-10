"""会话展示记录与前端回放：以后端记录为准，不重建模型历史。"""

from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree
import asyncio
import copy
import json
import threading
import time

import pytest

from agent_harness.frontends.cli import CLIAdapter
from agent_harness.runtime.interaction import InteractionChannel
from support.fakes import blocking_client, response, tool
from support.helpers import (
    complete,
    displayed_approval,
    records,
    stored,
    until,
    wait_stored,
)
import agent_harness.context.compaction as compaction
import agent_harness.core as core
import agent_harness.runtime.scheduler as scheduler
import agent_harness.tools.file_tools as file_tools
import agent_harness.workspace.tasks as tasks


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
    assert runtime.lead_context.todos == todos
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
    assert result["payload"]["status"] == "success"
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


def _save_trace(monkeypatch, runtime):
    """记录每次落盘时快照里最后一条展示记录的类型。"""
    trace = []
    original = runtime.session.save

    def counting():
        events = runtime.session.snapshot["events"] if runtime.session.snapshot else []
        trace.append(events[-1]["type"] if events else None)
        return original()

    monkeypatch.setattr(runtime.session, "save", counting)
    return trace


def test_turn_boundary_saves_terminal_fact_before_delivery(runtime_factory, monkeypatch):
    """C4 验收：简单回合保存 4 次，且终态先落盘再交付。

    保存点依次落在模型开始、助手消息接受、模型调用结束与回合终态；最后一次
    保存已包含 turn_finished，前端读到的终态与文件一致。
    """
    runtime, host = runtime_factory([response("Done")])
    trace = _save_trace(monkeypatch, runtime)
    finished, _ = complete(runtime, "run")
    assert trace == ["execution_status", "assistant_message", "execution_status", "turn_finished"]
    assert finished.payload["saved"] is True
    assert stored(runtime)["events"][-1]["type"] == "turn_finished"


def test_tool_turn_boundary_saves_each_settled_step(runtime_factory, monkeypatch):
    """C4 验收：带一次工具往返的回合保存 9 次，逐项结算步骤各落盘一次。"""
    runtime, host = runtime_factory([
        response("Before", tools=[tool("bash", command="printf hi")], stop="tool_use"),
        response("After"),
    ])
    trace = _save_trace(monkeypatch, runtime)
    submission = runtime.submit("run command")
    request, _ = displayed_approval(runtime)
    assert runtime.channel.respond(request.request_id, request.generation, "approve")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert trace == ["execution_status", "tool_call", "approval_requested", "approval_decided",
                     "tool_result", "tool_result", "assistant_message", "execution_status",
                     "turn_finished"]
    assert finished.payload["saved"] is True


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
    original = file_tools.run_bash
    observed = []

    def execute(*args, **kwargs):
        snapshot = stored(runtime)
        assert records(snapshot, "tool_call")[0]["payload"]["id"] == "call"
        assert records(snapshot, "approval_requested")
        assert records(snapshot, "approval_decided")[0]["payload"]["decision"] == "approve"
        assert not records(snapshot, "tool_result")
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(file_tools, "run_bash", execute)
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

    monkeypatch.setattr(compaction, "prepare_history", compact)
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
    monkeypatch.setattr(core.client.messages, "create", lambda **kwargs: model_calls.append(kwargs))
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
    assert not (core.WORKDIR / "must-not-exist").exists()
    resume(other)
    changed = resume(target)
    historical = [item for item in changed.payload["records"] if item["type"] != "command_result"]
    assert historical == [item for item in first if item["type"] != "command_result"]
    assert not model_calls
    monkeypatch.setattr(core.client.messages, "create", lambda **kwargs: response("Continued safely"))
    complete(runtime, "continue from saved state")
    assert not (core.WORKDIR / "must-not-exist").exists()
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

    monkeypatch.setattr(file_tools, "_run_bash_process", process)
    original_wait = scheduler.wait_for_background_tasks

    def wait_background():
        release.set()
        original_wait()

    monkeypatch.setattr(scheduler, "wait_for_background_tasks", wait_background)
    with host.agent_lock, execution_context(runtime._context(turn_id="background-final", origin="background")):
        task_id = scheduler.start_background_task(tool("bash", command="wait offline"), {})
    assert started.wait(2)
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    states = [item["payload"]["status"] for item in records(stored(runtime), "background_status")
              if item["payload"]["task_id"] == task_id]
    assert states == ["running", "completed"]
    results = records(stored(runtime), "tool_result")
    assert any(item["payload"]["tool_use_id"] == "call"
               and item["payload"]["status"] == "success" for item in results)
    assert not scheduler.has_pending_background()


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


@pytest.mark.parametrize("invalid", [
    None,
    {"content": "bad", "status": "pending"},
    '[{"content":"bad","status":"pending"}]',
    "[{'content': 'bad', 'status': 'pending'}]",
    ["bad"],
    [{"content": "bad"}],
    [{"status": "pending"}],
    [{"content": 42, "status": "pending"}],
    [{"content": "bad", "status": "unknown"}],
    [{"content": "valid prefix", "status": "pending"}, {"content": None, "status": "pending"}],
])
def test_todo_records_include_only_applied_updates(runtime_factory, invalid):
    todos = [{"content": "Check output", "status": "in_progress"}]
    values = iter([
        response(tools=[tool("todo_write", "valid", todos=todos),
                        tool("todo_write", "invalid", todos=invalid)],
                 stop="tool_use"), response("Done")])
    requests = []

    def model(**kwargs):
        requests.append(copy.deepcopy(kwargs["messages"]))
        return next(values)

    runtime, host = runtime_factory(client=model)
    complete(runtime, "track progress")
    updates = records(stored(runtime), "todo_updated")
    assert len(updates) == 1
    assert updates[0]["payload"]["todos"] == todos
    assert stored(runtime)["todos"] == todos
    assert runtime.lead_context.todos == todos
    results = requests[1][-1]["content"]
    assert [item["tool_use_id"] for item in results] == ["valid", "invalid"]
    assert results[0]["content"] == "已更新 1 项待办：完成 0 项，进行中 1 项，待处理 0 项。"
    assert results[1]["content"].startswith("Error: ")


def test_model_owns_todo_updates_without_cross_round_reminders(runtime_factory):
    todos = [
        {"content": "准备文件", "status": "completed"},
        {"content": "读取文件", "status": "in_progress"},
        {"content": "总结结果", "status": "pending"},
    ]
    completed = [dict(todo, status="completed") for todo in todos]
    values = iter([
        response(tools=[tool("todo_write", "plan", todos=todos)], stop="tool_use"),
        # 分六轮执行真实普通工具，覆盖跨模型轮次的旧提醒触发条件。
        *[response(tools=[tool("read_file", f"read-{index}",
                              path="输入.txt" if index % 2 == 0 else "不存在.txt")], stop="tool_use")
          for index in range(6)],
        response(tools=[tool("todo_write", "finish-plan", todos=completed)], stop="tool_use"),
        response(tools=[tool("todo_write", "clear-plan", todos=[])], stop="tool_use"),
        response("Done"),
    ])
    requests, observed = [], []

    def model(**kwargs):
        requests.append(copy.deepcopy(kwargs["messages"]))
        observed.append(copy.deepcopy(runtime.lead_context.todos))
        return next(values)

    runtime, host = runtime_factory(client=model)
    (core.WORKDIR / "输入.txt").write_text("读取成功", encoding="utf-8")
    finished, _ = complete(runtime, "读取文件并记录计划")
    assert finished.payload["outcome"]["status"] == "completed"
    assert len(requests) == 10
    assert observed == [[], *[todos for _ in range(7)], completed, []]
    expected_ids = ["plan", *[f"read-{index}" for index in range(6)], "finish-plan", "clear-plan"]
    for index, call_id in enumerate(expected_ids, start=1):
        # 每轮只追加助手响应和配对结果，模型历史里没有额外提醒消息。
        assert len(requests[index]) == 1 + index * 2
        results = requests[index][-1]["content"]
        assert len(results) == 1
        assert results[0]["type"] == "tool_result"
        assert results[0]["tool_use_id"] == call_id
        assert "<reminder>" not in str(requests[index])
    assert requests[1][-1]["content"][0]["content"] == "已更新 3 项待办：完成 1 项，进行中 1 项，待处理 1 项。"
    for index in range(6):
        output = requests[index + 2][-1]["content"][0]["content"]
        if index % 2 == 0:
            assert "读取成功" in output
        else:
            assert output.startswith("Error:")
    assert requests[8][-1]["content"][0]["content"] == "已更新 3 项待办：完成 3 项，进行中 0 项，待处理 0 项。"
    assert requests[9][-1]["content"][0]["content"] == "已更新 0 项待办：完成 0 项，进行中 0 项，待处理 0 项。"
    updates = records(stored(runtime), "todo_updated")
    assert [item["payload"]["todos"] for item in updates] == [todos, completed, []]
    assert stored(runtime)["todos"] == runtime.lead_context.todos == []


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
    assert not (core.WORKDIR / "must-not-exist").exists()


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
    runtime, _ = runtime_factory(client=blocking_client(started, release))
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
    (core.WORKDIR / "first.txt").write_text("first result", encoding="utf-8")
    original = file_tools.run_read
    def read(path, **kwargs):
        if str(path).endswith("second.txt"):
            entered.set()
            assert release.wait(10)
        return original(path, **kwargs)
    monkeypatch.setattr(file_tools, "run_read", read)
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
    (core.WORKDIR / "saved.txt").write_text("saved result", encoding="utf-8")
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


def record(number, kind, payload, source="lead", turn="turn"):
    return {"id": f"{number:032x}", "at": "2026-10-09T00:00:00+00:00", "kind": "display",
            "type": kind, "source": source, "turn_id": turn, "payload": payload}


def event(kind, payload):
    return SimpleNamespace(type=kind, payload=payload, scope="session", generation=1,
                           session_id="test", sequence=1, turn_id="turn", source="lead")


def test_cli_record_replay_details_status_and_dedup():
    output = []
    adapter = CLIAdapter(None, write=output.append)
    records = [record(1, "user_message", {"text": "request"}),
               record(2, "tool_call", {"id": "tool", "name": "read_file", "input": {"path": "文件.txt"}}),
               record(3, "tool_result", {"tool_use_id": "tool", "content": "Error is normal content\n" + "x" * 500}),
               record(4, "todo_updated", {"todos": [{"status": "completed", "content": "inspect"}]}),
               record(5, "approval_requested", {"request_id": "historical", "tool_name": "bash", "arguments": {},
                                                 "historical": True, "interrupted": True}),
               record(6, "turn_finished", {"outcome": {"status": "completed"}, "summary": {
                   "elapsed_seconds": 1.5, "tool_calls": 1, "approvals": 0,
                   "lead_usage": {"model_calls": 2, "input_tokens": 20, "output_tokens": 10}}})]
    adapter.render(event("session_changed", {"records": records}))
    adapter.render(event("display_record", {"record": records[2]}))
    adapter.render(event("assistant_message", {"text": "raw duplicate"}))
    text = "\n".join(output)
    assert len(output) == len(records)
    assert "read_file · 已返回结果" in text and "Error is normal content" in text and "x" * 500 not in text
    assert "文件.txt" in text and "上次中断，未继续执行" in text
    assert "raw duplicate" not in text
    assert "1.5s" in text and "请求 2" in text
    missing = record(7, "tool_call", {"id": "unfinished", "name": "write_file", "input": {},
                                       "historical": True, "result_missing": True})
    adapter.render(event("display_record", {"record": missing}))
    assert output[-1] == "结果未记录，请核对当前状态"


class DisplayRuntime:
    def __init__(self):
        self.channel = InteractionChannel()
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


def test_tui_record_window_expansion_history_and_dedup():
    pytest.importorskip("textual")
    from textual.widgets import Button, Collapsible, Static
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import ApprovalCard, ConversationRecord

    app = HarnessApp(DisplayRuntime())
    records = [record(index + 1, "user_message", {"text": f"message {index}"}) for index in range(90)]
    records += [record(91, "tool_call", {"id": "tool", "name": "read_file", "input": {"path": "文件.txt"},
                                        "result_missing": True}),
                record(92, "tool_result", {"tool_use_id": "tool", "content": "detailed result", "status": "success"}),
                record(93, "approval_requested", {"request_id": "old", "tool_name": "bash", "arguments": {},
                                                   "historical": True, "interrupted": True})]

    async def exercise():
        async with app.run_test(size=(55, 22)) as pilot:
            await app._event(event("session_changed", {"records": records}))
            await pilot.pause()
            assert len(app.query(ConversationRecord)) == 80
            assert app.query_one("#earlier", Button).display
            assert not app.query_one(ApprovalCard).query(Button)
            details = app.query(Collapsible)
            assert len(details) == 3 and all(detail.collapsed for detail in details)
            assert any("结果未记录" in str(widget.content) for widget in app.query(Static))
            details[1].collapsed = False
            await pilot.pause()
            assert any("detailed result" in str(widget.content) for widget in details[1].query(Static))
            await app._event(event("display_record", {"record": records[-1]}))
            assert len(app.records) == 93
            await pilot.click("#earlier")
            await pilot.pause()
            assert len(app.query(ConversationRecord)) == 93
            assert not app.query_one("#earlier", Button).display
            assert app.query_one("#input").region.bottom <= 22
    asyncio.run(exercise())


def test_tui_workbench_has_brand_state_panel_and_single_stateful_button():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import BrandBar, SessionStatePanel
    from textual.widgets import Button, Static

    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(110, 30)) as pilot:
            assert app.query_one(BrandBar)
            assert app.query_one(SessionStatePanel)
            assert app.query_one("#send", Button).label == "发送"
            assert not app.query("#stop")
            await app._event(event("state_snapshot", {
                "history": [{"role": "user", "content": "hello"}],
                "goal": None, "todos": [], "subagents": [], "model": "test-model",
                "busy": True, "closing": False,
                "lead_main_model_usage": {"model_calls": 1, "input_tokens": 2, "output_tokens": 3},
                "workdir": "/tmp/project", "task_id": "task_1", "worktree": "feature",
            }))
            await pilot.pause()
            assert app.query_one("#send", Button).label == "停止"
            state = str(app.query_one(SessionStatePanel).query_one(".state-body", Static).content)
            assert "test-model" in state and "/tmp/project" in state and "feature" in state
    import asyncio
    asyncio.run(exercise())


def test_tui_inline_approval_only_enables_delivered_request(monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import ApprovalCard
    from agent_harness.runtime.interaction import ApprovalRequest

    runtime = DisplayRuntime()
    app = HarnessApp(runtime)
    ready = []
    decisions = []
    monkeypatch.setattr(runtime.channel, "next_approval", lambda: ready.pop(0) if ready else None)
    monkeypatch.setattr(runtime.channel, "respond", lambda *args: decisions.append(args) or True)
    request = ApprovalRequest("current", "test", 1, "turn", "lead", "bash", "tool", {"command": "pwd"})

    async def exercise():
        async with app.run_test(size=(45, 18)) as pilot:
            await app.conversation.display_record(record(1, "approval_requested", {"request_id": "old", "tool_name": "bash",
                                      "arguments": {}, "historical": True, "interrupted": True}))
            await app.conversation.display_record(record(2, "approval_requested", {"request_id": "current", "tool_name": "bash",
                                      "arguments": {"command": "pwd"}}))
            await pilot.pause()
            card = next(card for card in app.query(ApprovalCard) if card.request_id == "current")
            assert all(button.disabled for button in card.query(Button))
            ready.append(request)
            await app.consume()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert not card.query_one(".approve", Button).disabled
            assert card.query_one(".approve", Button).region.y >= 2
            assert card.query_one(".approve", Button).region.bottom <= app.query_one("#input").region.y
            image = ElementTree.fromstring(app.export_screenshot())
            screen_text = "".join(image.itertext())
            assert "批准" in screen_text and "拒绝" in screen_text, screen_text
            await pilot.click(card.query_one(".approve", Button))
            await pilot.pause()
            assert decisions == [("current", 1, "approve")]
            assert all(button.disabled for button in card.query(Button))
            assert app.approval is None
    asyncio.run(exercise())
def test_tool_summary_is_visible_before_expanding_details():
    from agent_harness.frontends.tui.app import HarnessApp
    from textual.widgets import Collapsible
    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            await app.conversation.display_record(record(1, "tool_call", {"id": "read", "name": "read_file",
                                     "input": {"path": "说明.md"}}))
            await app.conversation.display_record(record(2, "tool_result", {"tool_use_id": "read", "status": "returned",
                                     "content": "读取成功\n" + "long output\n" * 100}))
            await pilot.pause()
            text = "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert "说明.md" in text and "读取成功" in text
            assert all(item.collapsed for item in app.query(Collapsible))
    asyncio.run(exercise())


def test_operation_summary_keeps_confirmed_elapsed_time():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import OperationEntry
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            from agent_harness.frontends.conversation import ConversationProjection
            projection = ConversationProjection()
            change = projection.apply_record(record(1, "execution_status", {
                "operation_id": "op", "phase": "tool", "name": "读取 README.md",
                "state": "completed", "elapsed_seconds": 3.0,
            }))[0]
            entry = OperationEntry(change["entry"])
            await app.query_one("#chat", VerticalScroll).mount(entry)
            entry.render_entry(change["entry"])
            await pilot.pause()
            text = str(entry.query_one(".operation-summary", Static).content)
            assert "耗时 3.0s" in text
    import asyncio
    asyncio.run(exercise())
def test_many_tools_share_scrollable_group_and_update_in_place():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import OperationEntry, ProcessGroup
    from textual.containers import VerticalScroll
    from textual.events import MouseScrollDown
    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(85, 32)) as pilot:
            for index in range(20):
                await app.conversation.display_record(record(100 + index, "tool_call", {
                    "id": f"tool-{index}", "name": "read_file", "input": {"path": f"file-{index}.txt"},
                    "model_call_id": "model-1"}))
                await app.conversation.display_record(record(200 + index, "tool_result", {
                    "tool_use_id": f"tool-{index}", "content": f"result-{index}", "status": "returned",
                    "model_call_id": "model-1"}))
            await pilot.pause()
            assert len(app.query(ProcessGroup)) == 1
            assert len(app.query(OperationEntry)) == 20
            scroll = app.query_one(ProcessGroup).query_one(VerticalScroll)
            assert scroll.region.height <= 14 and scroll.max_scroll_y > 0
            scroll.scroll_home(animate=False)
            await pilot.pause()
            await pilot.hover(scroll, offset=(3, 2))
            outer = app.query_one("#chat", VerticalScroll)
            position = outer.scroll_y
            await pilot._post_mouse_events([MouseScrollDown], scroll, offset=(3, 2))
            await pilot.pause()
            assert scroll.scroll_y > 0 and outer.scroll_y == position
            bar = scroll.vertical_scrollbar
            await pilot.click(bar, offset=(0, bar.region.height - 2))
            await pilot.pause()
            assert scroll.scroll_y > 0
            scroll.scroll_end(animate=False)
            await pilot.pause()
            text = "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert "file-19.txt" in text and "result-19" in text
    asyncio.run(exercise())
def test_cli_stream_confirms_without_duplicate_body_and_marks_discarded_attempt():
    output, chunks = [], []
    adapter = CLIAdapter(None, write=output.append, write_chunk=chunks.append)
    adapter.render(event("assistant_text_delta", {"model_call_id": "one", "block_index": 0, "sequence": 1, "delta": "你好"}))
    adapter.render(event("log", {"text": "background update"}))
    adapter.render(event("assistant_text_delta", {"model_call_id": "one", "block_index": 0, "sequence": 2, "delta": "完成"}))
    adapter.render(event("display_record", {"record": record(1, "assistant_message", {
        "model_call_id": "one", "block_index": 0, "text": "你好完成"})}))
    adapter.render(event("display_record", {"record": record(2, "execution_status", {
        "phase": "model", "name": "请求模型", "state": "discarded", "operation_id": "old", "model_call_id": "one"})}))
    assert "你好" in "".join(chunks) and "完成" in "".join(chunks)
    assert not any("你好完成" in text for text in output)
    assert sum("本次输出未采用" in text for text in output) == 1
    assert output[0] == "background update"


def test_projection_confirmed_body_rejects_late_delta_and_is_read_only():
    from agent_harness.frontends.conversation import ConversationProjection
    projection = ConversationProjection()
    projection.restore([], "test", 1)
    projection.apply_record(record(1, "assistant_message", {"model_call_id": "one", "text": "正式正文"}))
    assert projection.apply_delta(event("assistant_text_delta", {
        "model_call_id": "one", "block_index": 0, "sequence": 1, "delta": "过期正文"})) == []
    records = projection.records
    records[0]["payload"]["text"] = "外部修改"
    assert projection.records[0]["payload"]["text"] == "正式正文"
    projection.apply_record(record(2, "execution_status", {
        "phase": "model", "name": "请求模型", "state": "failed", "operation_id": "op", "model_call_id": "two"}))
    assert projection.apply_delta(event("assistant_text_delta", {
        "model_call_id": "two", "block_index": 0, "sequence": 1, "delta": "结束后迟到"})) == []


def test_projection_deduplicates_and_discards_only_matching_unaccepted_source():
    from agent_harness.frontends.conversation import ConversationProjection
    projection = ConversationProjection()
    delta = event("assistant_text_delta", {"model_call_id": "same", "block_index": 0, "sequence": 1, "delta": "Lead"})
    projection.apply_delta(delta)
    assert projection.apply_delta(delta) == []
    delta.source = "child"
    delta.payload = dict(delta.payload, delta="Child")
    projection.apply_delta(delta)
    changes = projection.apply_record(record(1, "execution_status", {
        "phase": "model", "name": "请求模型", "state": "failed", "operation_id": "op", "model_call_id": "same"}))
    assert [c["entry"]["text"] for c in changes if c["kind"] == "discarded"] == ["Lead"]
    delta.generation = 0
    assert projection.apply_delta(delta) == []
    assert next(e for e in projection.entries if e.get("source") == "child")["discarded"] is False


def test_tui_window_rebuild_preserves_expansion_and_live_body():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import OperationEntry, StreamingText
    from textual.widgets import Collapsible, Markdown
    app = HarnessApp(DisplayRuntime())

    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            await app.conversation.display_record(record(1, "tool_call", {
                "id": "tool", "name": "read_file", "input": {"path": "说明.md"}, "model_call_id": "model"}))
            app.query_one(OperationEntry).query_one(Collapsible).collapsed = False
            await app._event(event("assistant_text_delta", {
                "model_call_id": "next", "block_index": 0, "sequence": 1, "delta": "活动正文"}))
            await app.conversation.load_earlier()
            await pilot.pause()
            assert not app.query_one(OperationEntry).query_one(Collapsible).collapsed
            assert app.query_one(StreamingText).query_one(Markdown)._markdown == "活动正文"
            await app.conversation.display_record(record(2, "assistant_message", {
                "model_call_id": "next", "block_index": 0, "text": "正式正文"}))
            await app.conversation.load_earlier()
            await pilot.pause()
            assert app.query_one(StreamingText).query_one(Markdown)._markdown == "正式正文"
            failed = event("assistant_text_delta", {
                "model_call_id": "failed", "block_index": 0, "sequence": 1, "delta": "未采用正文"})
            await app._event(failed)
            await app.conversation.display_record(record(3, "execution_status", {
                "phase": "model", "name": "请求模型", "state": "failed", "operation_id": "failed", "model_call_id": "failed"}))
            streams = list(app.query(StreamingText))
            streams[-1].query_one(Collapsible).collapsed = False
            await app.conversation.load_earlier()
            await pilot.pause()
            assert not list(app.query(StreamingText))[-1].query_one(Collapsible).collapsed
    asyncio.run(exercise())
