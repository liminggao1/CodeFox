"""会话展示记录与前端回放：以后端记录为准，不重建模型历史。"""

from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree
import asyncio
import copy
import json
import threading
import uuid

import pytest

from codefox.frontends.cli import CLIAdapter
from codefox.runtime.interaction import InteractionChannel
from support.fakes import blocking_client, response, tool
from support.helpers import (
    complete,
    displayed_approval,
    records,
    stored,
    until,
)
import codefox.context.compaction as compaction
import codefox.core as core
import codefox.runtime.scheduler as scheduler
import codefox.tools.file_tools as file_tools


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
    for status in ("idle", "budget_exhausted", "network_error"):
        runtime.session.snapshot["events"].append({
            "id": status, "kind": "display", "type": "subagent_status", "source": "helper",
            "turn_id": "turn", "payload": {"name": "helper", "mode": "persistent", "status": status},
        })
    historical = runtime.session.display_records(historical=True)
    assert [item["payload"]["status"] for item in historical[3:]] == [
        "idle", "budget_exhausted", "network_error"]


def test_saved_records_match_live_order_and_contain_full_tool_payload(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([
        response("Before", tools=[tool("bash", command="printf 'Error: sample output'")], stop="tool_use"),
        response("After"),
    ])
    observed = []
    execute = file_tools.run_bash

    def checked_execute(*args, **kwargs):
        checkpoint = stored(runtime)
        assert records(checkpoint, "tool_call")[0]["payload"]["id"] == "call"
        assert records(checkpoint, "approval_requested")
        assert records(checkpoint, "approval_decided")[0]["payload"]["decision"] == "approve"
        assert not records(checkpoint, "tool_result")
        observed.append(True)
        return execute(*args, **kwargs)

    monkeypatch.setattr(file_tools, "run_bash", checked_execute)
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
    assert observed == [True]
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
    assert not any(item["payload"].get("phase") == "hook" for item in saved)
    logs = [event.payload["text"] for event in before + after if event.type == "log"]
    for hook_event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        assert any(f"[HOOK] {hook_event} / " in text and "开始" in text for text in logs)
        assert any(f"[HOOK] {hook_event} / " in text and "完成" in text for text in logs)
    assert any("[HOOK] bash" in text for text in logs)
    assert any("[HOOK] Stop: 1 tool result(s)" in text for text in logs)


def test_compaction_changes_model_messages_without_erasing_display_history(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([response("Original reply"), response("New reply")])
    complete(runtime, "original question")
    prior = records(stored(runtime))
    from codefox.context.compaction import CompactionResult
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


def test_invalid_current_context_is_rejected_without_modifying_files_or_active_session(runtime_factory):
    runtime, host = runtime_factory([response("Current answer")])
    complete(runtime, "current question")
    active_id = runtime.session.session_id
    active_history = copy.deepcopy(runtime.session.history)
    active_generation = runtime._generation
    valid_snapshot = copy.deepcopy(stored(runtime))
    for malformation in (
        "missing_display", "missing_reply", "missing_kind", "invalid_payload",
        "invalid_text", "duplicate_id", "invalid_summary", "invalid_outcome", "invalid_todo",
    ):
        snapshot = copy.deepcopy(valid_snapshot)
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
        assert result.payload["status"] == "failed", malformation
        assert path.read_bytes() == original_bytes, malformation
        assert runtime.session.session_id == active_id, malformation
        assert runtime.session.history == active_history, malformation
        assert runtime._generation == active_generation, malformation
        assert not any(event.type == "session_changed" for event in events), malformation


def test_shutdown_saves_background_terminal_status_after_worker_join(runtime_factory, monkeypatch):
    from codefox.runtime.interaction import execution_context

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


def test_todo_records_include_only_applied_updates(runtime_factory):
    todos = [{"content": "Check output", "status": "in_progress"}]
    values = iter([
        response(tools=[tool("todo_write", "valid", todos=todos),
                        tool("todo_write", "invalid-status", todos=[
                            {"content": "bad", "status": "unknown"}]),
                        tool("todo_write", "invalid-content", todos=[
                            {"content": "valid prefix", "status": "pending"},
                            {"content": None, "status": "pending"}])],
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
    detached = runtime.lead_context.snapshot()
    detached["todos"].clear()
    assert runtime.lead_context.todos == todos
    results = requests[1][-1]["content"]
    assert [item["tool_use_id"] for item in results] == [
        "valid", "invalid-status", "invalid-content",
    ]
    assert results[0]["content"] == "已更新 1 项待办：完成 0 项，进行中 1 项，待处理 0 项。"
    assert all(item["content"].startswith("Error: ") for item in results[1:])
    # The state owner also preserves completion, clearing and input isolation.
    for malformed in (None, [None], [{"content": "missing status"}]):
        with pytest.raises(ValueError):
            runtime.lead_context.replace_todos(malformed)
        assert runtime.lead_context.todos == todos, malformed
    completed = [{"content": "Check output", "status": "completed"}]
    assert runtime.lead_context.replace_todos(completed) == completed
    completed[0]["content"] = "mutated input"
    assert runtime.lead_context.todos == [{"content": "Check output", "status": "completed"}]
    assert runtime.lead_context.replace_todos([]) == []
    assert runtime.lead_context.todos == []


def test_background_fact_is_visible_before_blocked_lead_checkpoint(runtime_factory):
    from codefox.runtime.interaction import ExecutionContext

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


def record(number, kind, payload, source="lead", turn="turn"):
    return {"id": f"{number:032x}", "at": "2026-10-09T00:00:00+00:00", "kind": "display",
            "type": kind, "source": source, "turn_id": turn, "payload": payload}


def event(kind, payload):
    return SimpleNamespace(type=kind, payload=payload, scope="session", generation=1,
                           session_id="test", sequence=1, turn_id="turn", source="lead",
                           id=uuid.uuid4().hex, at="2026-10-09T00:00:00+00:00")


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
    assert "[lead] read_file" in text and "Error is normal content" in text and "x" * 500 not in text
    assert "文件.txt" in text and "上次中断，未继续执行" in text
    assert "raw duplicate" not in text
    assert "1.5s" in text and "请求 2" in text
    missing = record(7, "tool_call", {"id": "unfinished", "name": "write_file", "input": {},
                                       "historical": True, "result_missing": True})
    adapter.render(event("display_record", {"record": missing}))
    assert output[-1] == "结果未记录，请核对当前状态"


class DisplayRuntime:
    permission_mode = "default"

    def __init__(self):
        self.channel = InteractionChannel()
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


def test_tui_subagent_progress_updates_one_row_and_preserves_records():
    pytest.importorskip("textual")
    from textual.widgets import Static
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import ConversationRecord

    app = HarnessApp(DisplayRuntime())
    updates = [record(1, "user_message", {"text": "Summarize README"})]
    updates += [record(index + 2, "subagent_status", {
        "agent_id": "child-1", "name": "readme_summarizer", "mode": "once",
        "status": "working", "rounds": index // 5, "tool_calls": index // 6,
    }, source="readme_summarizer") for index in range(11)]
    updates.append(record(13, "assistant_message", {"text": "README summary"}))
    updates.append(record(14, "subagent_status", {
        "agent_id": "child-1", "name": "readme_summarizer", "mode": "once",
        "status": "completed", "rounds": 2, "tool_calls": 1,
    }, source="readme_summarizer"))

    def status_row():
        rows = [widget for widget in app.query(ConversationRecord)
                if widget.record["type"] == "subagent_status"]
        assert len(rows) == 1
        assert [str(widget.content) for widget in rows[0].query(Static)] == [
            "readme_summarizer · completed"]
        assert rows[0].record == updates[-1]
        return rows[0]

    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            for update in updates[:-1]:
                await app.conversation.display_record(update)
            initial = next(
                widget for widget in app.query(ConversationRecord) if widget.record["type"] == "subagent_status")
            await app.conversation.display_record(updates[-1])
            assert status_row() is initial
            await pilot.pause()
            status_row()
            assert app.records == updates
            await app.conversation.restore(updates, session_id="test", generation=1)
            status_row()
            await app.conversation.render_window()
            await pilot.pause()
            status_row()
            assert app.records == updates
            await app.conversation.display_record(record(15, "subagent_status", {
                "agent_id": "child-2", "name": "readme_summarizer", "mode": "once", "status": "working",
            }, source="readme_summarizer"))
            assert len([widget for widget in app.query(ConversationRecord)
                        if widget.record["type"] == "subagent_status"]) == 2
            await app.conversation.restore([], session_id="new", generation=2)
            assert not app.query(ConversationRecord)
    asyncio.run(exercise())


def test_tui_record_window_expansion_history_and_dedup():
    pytest.importorskip("textual")
    from textual.widgets import Button, Collapsible, Static
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import ApprovalCard, ConversationRecord

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


def test_tui_inline_approval_only_enables_delivered_request(monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import ApprovalCard
    from codefox.runtime.interaction import ApprovalRequest

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


def test_projection_deduplicates_and_discards_only_matching_unaccepted_source():
    from codefox.frontends.conversation import ConversationProjection
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
    projection.apply_record(record(2, "assistant_message", {"model_call_id": "accepted", "text": "正式正文"}))
    confirmed = event("assistant_text_delta", {
        "model_call_id": "accepted", "block_index": 0, "sequence": 1, "delta": "迟到正文"})
    assert projection.apply_delta(confirmed) == []
    detached = projection.records
    detached[-1]["payload"]["text"] = "外部修改"
    assert projection.records[-1]["payload"]["text"] == "正式正文"


def test_tui_window_rebuild_preserves_expansion_and_live_body():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import OperationEntry, StreamingText
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


def test_tui_state_panel_toggles_in_one_layout_and_keeps_input_visible():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import BrandBar, SessionStatePanel
    from textual.widgets import Button

    app = HarnessApp(DisplayRuntime())

    async def exercise():
        async with app.run_test(size=(72, 24)) as pilot:
            await pilot.pause()
            # 只有一套布局：品牌栏固定 1 行，没有窄屏专用入口。
            assert app.query_one(BrandBar).region.height == 1
            assert [button.id for button in app.query_one(BrandBar).query(Button)] == [
                "history", "state", "new-session", "settings"]
            assert not app.query_one(SessionStatePanel).display
            await pilot.click("#state")
            await pilot.pause()
            panel = app.query_one(SessionStatePanel)
            assert panel.display and panel.region.width == 32
            # 对话列保留，输入框不被压缩且仍在屏幕内。
            assert app.query_one("#conversation").display
            assert app.query_one("#input").region.width >= 40
            assert app.query_one("#input-box").region.bottom <= 24
            await pilot.click("#state-collapse")
            await pilot.pause()
            assert not panel.display
    asyncio.run(exercise())


def test_tui_settings_dialog_toggles_diagnostics_and_returns_to_draft():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import SettingsScreen
    from textual.screen import ModalScreen
    from textual.widgets import Switch, TextArea

    app = HarnessApp(DisplayRuntime())
    size = (72, 24)

    async def exercise():
        async with app.run_test(size=size) as pilot:
            main_screen = app.screen
            editor = app.query_one("#input", TextArea)
            editor.load_text("保留草稿")
            await pilot.click("#settings")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen) and isinstance(app.screen, ModalScreen)
            dialog = app.screen.query_one("Vertical")
            assert dialog.region.width < size[0] and dialog.region.height < size[1]
            switch = app.screen.query_one("#diagnostics-toggle", Switch)
            assert switch.value and app.conversation.diagnostics_visible
            await pilot.click("#diagnostics-toggle")
            await pilot.pause()
            assert not app.conversation.diagnostics_visible
            # 设置弹窗打开时运行时日志仍能更新，关闭后隐藏日志但保留草稿。
            await app._event(event("log", {"text": "设置期间的日志"}))
            await pilot.click("#settings-back")
            await pilot.pause()
            assert app.screen is main_screen
            assert not app.query_one(".diagnostic").display
            assert editor.text == "保留草稿" and app.focused is editor
            await pilot.click("#settings")
            await pilot.pause()
            assert not app.screen.query_one("#diagnostics-toggle", Switch).value
            await pilot.click("#diagnostics-toggle")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is main_screen and app.query_one(".diagnostic").display
            app.query_one(".diagnostic").collapsed = False
            await pilot.pause()
            screen = "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert "设置期间的日志" in screen
            # 显示选择跨会话保留，日志内容仍在切换时清空。
            await app._event(event("session_changed", {"records": []}))
            assert app.conversation.diagnostics_visible and not app.query(".diagnostic")
            assert editor.text == "保留草稿"
    asyncio.run(exercise())


def test_tui_theme_switch_updates_reply_and_logs_without_losing_draft():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import SettingsScreen
    from codefox.frontends.tui.themes import DEFAULT_THEME, THEMES
    from textual.widgets import Markdown, Select, TextArea

    app = HarnessApp(DisplayRuntime())

    async def exercise():
        async with app.run_test(size=(112, 34)) as pilot:
            main_screen = app.screen
            editor = app.query_one("#input", TextArea)
            editor.load_text("保留配色切换前的草稿")
            await app._event(event("display_record", {"record": record(
                1, "assistant_message", {"text": "模型回复正文"})}))
            await app._event(event("log", {"text": "诊断日志正文"}))
            await pilot.pause()
            assert app.theme == DEFAULT_THEME
            await pilot.click("#settings")
            await pilot.pause()
            selector = app.screen.query_one("#theme-select", Select)
            # 真正打开下拉框，键盘切到第二项并确认。
            await pilot.click("#theme-select")
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.theme == "codefox-midnight"
            for theme in THEMES:
                selector.value = theme.name
                await pilot.pause()
                assert app.theme == theme.name
                reply = main_screen.query_one(Markdown)
                log = next(iter(app.conversation._diagnostic_details.values()))
                assert reply.styles.color.hex.lower() == theme.foreground
                assert log.styles.color.hex.lower() == theme.variables["codefox-muted"]
                assert reply.styles.color.brightness > log.styles.color.brightness
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is main_screen and app.focused is editor
            assert editor.text == "保留配色切换前的草稿"
            assert app.conversation.diagnostics_visible
            await app._event(event("session_changed", {"records": []}))
            assert app.theme == "codefox-warm"
            await pilot.click("#settings")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            assert app.screen.query_one("#theme-select", Select).value == "codefox-warm"
    asyncio.run(exercise())


def test_tui_rebuild_preserves_pending_input_activity_error_and_confirms_once():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.frontends.tui.widgets import ConversationRecord, MessageRow
    from textual.widgets import Collapsible, Static

    class SubmitRuntime(DisplayRuntime):
        def submit(self, text):
            return SimpleNamespace(status="accepted", kind="turn", submission_id="sub-1", reason="")

    app = HarnessApp(SubmitRuntime())
    notified = []
    app.notify = lambda message, **kwargs: notified.append(str(message))

    async def exercise():
        async with app.run_test(size=(112, 34)) as pilot:
            assert app._submit("待确认的消息")
            await pilot.pause()
            pending = app.query_one(MessageRow)
            assert "user" in pending.classes
            assert pending.query_one(ConversationRecord).region.x > 1
            await app._event(event("log", {"text": "保存暂不可用"}))
            await app._event(event("runtime_error", {"error": "保存暂不可用"}))
            await app._event(event("log", {"text": "当前会话日志"}))
            await pilot.pause()
            app.query_one(".diagnostic", Collapsible).collapsed = False
            await app.conversation.load_earlier()
            await pilot.pause()
            row = app.query_one(MessageRow)
            assert "user" in row.classes
            assert row.query_one(ConversationRecord).record["payload"]["text"] == "待确认的消息"
            assert str(app.query_one(".activity", Static).content) == "处理中"
            assert "保存暂不可用" in "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert not app.query_one(".diagnostic", Collapsible).collapsed
            assert app.records == [] and notified == ["保存暂不可用"]

            accepted = record(1, "user_message", {"text": "待确认的消息", "submission_id": "sub-1"})
            await app.conversation.display_record(accepted)
            await pilot.pause()
            assert list(app.query(MessageRow)) == [row]
            assert row.query_one(ConversationRecord).record == accepted
            app._status("完成")
            await app.conversation.load_earlier()
            await pilot.pause()
            assert len(app.query(MessageRow)) == 1 and not app.query(".activity")
            assert app.records == [accepted]

            # 切换会话会清空所有临时显示，旧日志和错误不会进入新会话。
            assert app._submit("切换前待确认的消息")
            await pilot.pause()
            assert len(app.query(MessageRow)) == 2
            await app._event(event("session_changed", {"records": []}))
            await pilot.pause()
            assert not app.query(MessageRow) and not app.query(".activity")
            assert not app.query(".diagnostic")
            assert not app.query_one("#chat").children
    asyncio.run(exercise())


def test_tui_diagnostics_follow_each_user_row_and_keep_late_child_logs_in_original_turn():
    from codefox.frontends.tui.app import HarnessApp

    app = HarnessApp(DisplayRuntime())

    async def exercise():
        async with app.run_test(size=(112, 34)) as pilot:
            for number in range(3):
                turn = f"turn-{number}"
                await app.conversation.display_record(record(number * 2 + 1, "user_message",
                                                             {"text": f"问题{number}"}, turn=turn))
                log = event("log", {"text": f"日志{number}", "origin": "frontend"})
                log.turn_id = turn
                await app._event(log)
                await app.conversation.display_record(record(number * 2 + 2, "assistant_message",
                                                             {"text": f"回答{number}"}, turn=turn))
            await pilot.pause()
            blocks = app.conversation._diagnostic_blocks
            assert len(blocks) == 3 and all(block.collapsed for block in blocks.values())
            children = list(app.query_one("#chat").children)
            for number in range(3):
                assert children[number * 3] is app.conversation._rows[f"{number * 2 + 1:032x}"]
                assert children[number * 3 + 1] is blocks[("turn", f"turn-{number}")]
                assert children[number * 3 + 2] is app.conversation._rows[f"{number * 2 + 2:032x}"]
            late = event("log", {"text": "第一轮子代理延迟日志", "origin": "persistent"})
            late.turn_id, late.source = "turn-0", "helper"
            await app._event(late)
            first = app.conversation._diagnostic_details[("turn", "turn-0")]
            assert "[helper] 第一轮子代理延迟日志" in str(first.content)
            assert "延迟日志" not in str(app.conversation._diagnostic_details[("turn", "turn-2")].content)
            blocks[("turn", "turn-0")].collapsed = False
            await app.conversation.load_earlier()
            assert not app.conversation._diagnostic_blocks[("turn", "turn-0")].collapsed
            assert app.conversation._diagnostic_blocks[("turn", "turn-1")].collapsed
            app.conversation.set_diagnostics_visible(False)
            await app._event(late)
            assert len(app.conversation._diagnostics) == 4
            assert all(not block.display for block in app.query(".diagnostic"))
            app.conversation.set_diagnostics_visible(True)
            assert all(block.display for block in app.query(".diagnostic"))
            assert "延迟日志" in str(app.conversation._diagnostic_details[("turn", "turn-0")].content)
    asyncio.run(exercise())


def test_tui_restored_logs_are_complete_and_follow_history_window():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.runtime.diagnostics import log_record

    app = HarnessApp(DisplayRuntime())
    history, logs = [], []
    for number in range(65):
        turn = f"turn-{number}"
        history.extend([record(number * 2 + 1, "user_message", {"text": f"问题{number}"}, turn=turn),
                        record(number * 2 + 2, "assistant_message", {"text": f"回答{number}"}, turn=turn)])
        log = event("log", {"text": f"日志{number}", "origin": "frontend"})
        log.turn_id = turn
        logs.append(log_record(log))
    long_text = "开头" + "x" * 9000 + "结尾"
    for number in range(1002):
        log = event("log", {"text": long_text if number == 0 else str(number), "origin": "frontend"})
        log.turn_id = "turn-64"
        logs.append(log_record(log))

    async def exercise():
        async with app.run_test(size=(112, 34)) as pilot:
            await app._event(event("session_changed", {"records": history, "diagnostics": logs}))
            assert len(app.query(".diagnostic")) == 40
            assert ("turn", "turn-0") not in app.conversation._diagnostic_blocks
            detail = app.conversation._diagnostic_details[("turn", "turn-64")]
            assert long_text in str(detail.content) and "[lead] 1001" in str(detail.content)
            assert len(app.conversation._diagnostics) == 1067
            app.conversation._diagnostic_blocks[("turn", "turn-64")].collapsed = False
            await app.conversation.load_earlier()
            assert len(app.query(".diagnostic")) == 65
            assert not app.conversation._diagnostic_blocks[("turn", "turn-64")].collapsed
            await app._event(event("session_changed", {"records": history, "diagnostics": logs}))
            assert all(block.collapsed for block in app.query(".diagnostic"))
            await app._event(event("session_changed", {"records": []}))
            assert not app.query(".diagnostic") and not app.conversation._diagnostics
    asyncio.run(exercise())


def test_tui_unanchored_logs_use_original_time_and_do_not_attach_to_next_user():
    from codefox.frontends.tui.app import HarnessApp
    from codefox.runtime.diagnostics import log_record

    app = HarnessApp(DisplayRuntime())
    first = record(1, "user_message", {"text": "第一轮"}, turn="first")
    first["at"] = "2026-10-09T00:00:00+00:00"
    last = record(2, "user_message", {"text": "下一轮"}, turn="last")
    last["at"] = "2026-10-09T00:03:00+00:00"
    background = event("log", {"text": "后台处理", "origin": "background"})
    background.turn_id, background.at = "background-turn", "2026-10-09T00:01:00+00:00"
    session = event("log", {"text": "会话维护", "origin": "frontend"})
    session.turn_id, session.at = None, "2026-10-09T00:02:00+00:00"

    async def exercise():
        async with app.run_test(size=(112, 34)) as pilot:
            await app._event(event("session_changed", {"records": [first, last],
                             "diagnostics": [log_record(background), log_record(session)]}))
            blocks = app.conversation._diagnostic_blocks
            children = list(app.query_one("#chat").children)
            assert children == [app.conversation._rows[first["id"]],
                                blocks[("background", "background-turn")], blocks[("session", None)],
                                app.conversation._rows[last["id"]]]
            assert blocks[("background", "background-turn")].title == "后台诊断日志"
            assert blocks[("session", None)].title == "会话诊断日志"
            assert ("turn", "last") not in blocks
            background.id = uuid.uuid4().hex
            background.payload["text"] = "后台延迟日志"
            background.at = "2026-10-09T00:04:00+00:00"
            await app._event(background)
            assert list(app.query_one("#chat").children) == children
            assert "后台延迟日志" in str(app.conversation._diagnostic_details[("background", "background-turn")].content)
    asyncio.run(exercise())
