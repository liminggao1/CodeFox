"""Diagnostics persist independently and follow their original conversation turn."""

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from agent_harness.frontends.cli import CLIAdapter
from agent_harness.agents.goal import GoalController
from agent_harness.runtime import interaction
from agent_harness.runtime.diagnostics import DiagnosticError, DiagnosticStore
from agent_harness.runtime.session_store import SessionError, SessionStore
from support.fakes import response
from support.helpers import complete, until


def diagnostic_event(session_id, text="诊断正文", turn="turn", source="lead"):
    return interaction.InteractionEvent(1, session_id, 1, turn, source, "log",
                                        {"text": text, "origin": "frontend"})


def test_diagnostics_store_complete_unicode_and_do_not_enumerate_as_sessions(tmp_path):
    sessions = SessionStore(tmp_path)
    snapshot = sessions.create()
    snapshot["goal"] = GoalController(None).snapshot()
    sessions.save(snapshot)
    store = DiagnosticStore(sessions.diagnostics_path)
    event = diagnostic_event(snapshot["id"], "完整开头\n" + "中文字" * 4000 + "\n完整结尾")
    assert store.capture(event) is None
    assert store.capture(event) is None
    path = sessions.diagnostics_path(snapshot["id"])
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r" not in path.read_bytes()
    saved = DiagnosticStore(sessions.diagnostics_path).read(snapshot["id"])
    assert len(saved) == 1 and saved[0]["text"] == event.payload["text"]
    entries, errors = sessions.list_sessions()
    assert [entry["id"] for entry in entries] == [snapshot["id"]] and not errors
    assert sessions.load(snapshot["id"])["events"] == []
    assert sessions.load(snapshot["id"])["messages"] == []


def test_missing_log_file_and_unknown_session_do_not_create_artifacts(tmp_path):
    sessions = SessionStore(tmp_path)
    snapshot = sessions.create()
    store = DiagnosticStore(sessions.diagnostics_path)
    assert store.read(snapshot["id"]) == []
    assert store.capture(diagnostic_event(None)) is None
    assert not sessions.diagnostics_path(snapshot["id"]).exists()


@pytest.mark.parametrize("content", [b"not json\n", b"{}\n", b"\xff\n"])
def test_corrupt_diagnostics_are_reported_and_preserved(tmp_path, content):
    sessions = SessionStore(tmp_path)
    snapshot = sessions.create()
    path = sessions.diagnostics_path(snapshot["id"])
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    store = DiagnosticStore(sessions.diagnostics_path)
    with pytest.raises(DiagnosticError, match=str(path)):
        store.read(snapshot["id"])
    assert store.capture(diagnostic_event(snapshot["id"], "新日志")) is None
    assert store.read(snapshot["id"])[0]["text"] == "新日志"
    assert path.read_bytes() == content


def test_failed_writes_keep_logs_retry_once_and_do_not_repeat_warning(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path)
    snapshot = sessions.create()
    store = DiagnosticStore(sessions.diagnostics_path)
    append = store._append
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(store, "_append", fail)
    first = diagnostic_event(snapshot["id"], "第一条")
    second = diagnostic_event(snapshot["id"], "第二条")
    assert "disk unavailable" in store.capture(first)
    assert store.capture(second) is None
    assert store.retry() == []
    assert [log["text"] for log in store.read(snapshot["id"])] == ["第一条", "第二条"]
    monkeypatch.setattr(store, "_append", append)
    assert store.retry() == []
    assert store.retry() == []
    restored = DiagnosticStore(sessions.diagnostics_path).read(snapshot["id"])
    assert [log["id"] for log in restored] == [first.id, second.id]


def test_partial_append_rolls_back_before_retry(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path)
    snapshot = sessions.create()
    store = DiagnosticStore(sessions.diagnostics_path)
    original_open = Path.open
    class PartialWrite:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def write(self, payload):
            self.handle.write(payload[:7])
            raise OSError("partial write")
    def open_file(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return PartialWrite(handle) if args and args[0] == "a+b" else handle
    monkeypatch.setattr(Path, "open", open_file)
    event = diagnostic_event(snapshot["id"])
    assert "partial write" in store.capture(event)
    assert sessions.diagnostics_path(snapshot["id"]).read_bytes() == b""
    monkeypatch.setattr(Path, "open", original_open)
    store.retry()
    assert len(DiagnosticStore(sessions.diagnostics_path).read(snapshot["id"])) == 1


@pytest.mark.parametrize("logs_exist", [True, False])
def test_delete_session_removes_only_bound_logs(tmp_path, logs_exist):
    sessions = SessionStore(tmp_path)
    snapshots = [sessions.create(), sessions.create()]
    for snapshot in snapshots:
        sessions.save(snapshot)
        if logs_exist:
            DiagnosticStore(sessions.diagnostics_path).capture(diagnostic_event(snapshot["id"]))
    sessions.delete(snapshots[0]["id"])
    assert not sessions.path(snapshots[0]["id"]).exists()
    assert not sessions.diagnostics_path(snapshots[0]["id"]).exists()
    assert sessions.path(snapshots[1]["id"]).exists()
    assert sessions.diagnostics_path(snapshots[1]["id"]).exists() == logs_exist


def test_log_delete_failure_preserves_session(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path)
    snapshot = sessions.create()
    sessions.save(snapshot)
    DiagnosticStore(sessions.diagnostics_path).capture(diagnostic_event(snapshot["id"]))
    original_unlink = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == sessions.diagnostics_path(snapshot["id"]):
            raise OSError("cannot delete log")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(SessionError, match="cannot delete log"):
        sessions.delete(snapshot["id"])
    assert sessions.path(snapshot["id"]).exists()
    assert sessions.diagnostics_path(snapshot["id"]).exists()


def test_logs_saved_during_model_request_and_restored_without_model_pollution(runtime_factory):
    started, release = threading.Event(), threading.Event()
    requests = []
    def create(**kwargs):
        requests.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        context = interaction.current_context()
        context.delegate("helper").publish("log", text="诊断专用正文")
        started.set()
        assert release.wait(3)
        return response("回答")
    runtime, _ = runtime_factory(client=create)
    submission = runtime.submit("你好")
    assert started.wait(2)
    session_id = runtime.session.session_id
    path = runtime.session.store.diagnostics_path(session_id)
    logs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert any(log["text"] == "诊断专用正文" and log["source"] == "helper" for log in logs)
    assert all(log["turn_id"] for log in logs)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    logs = runtime.diagnostic_records()
    assert sum("[TURN]" in log["text"] for log in logs) == 1
    assert "诊断专用正文" not in requests[0]
    snapshot = runtime.session.store.load(session_id)
    assert "诊断专用正文" not in json.dumps(snapshot, ensure_ascii=False)
    runtime.close()
    until(runtime, "runtime_closed")
    runtime._thread.join(3)
    logs = runtime.diagnostic_records()
    restored, _ = runtime_factory([response("继续回答")])
    restored.submit(f"/resume {session_id}")
    changed, _ = until(restored, "session_changed")
    assert [log["id"] for log in changed.payload["diagnostics"]] == [log["id"] for log in logs]
    assert any(log["turn_id"] == finished.turn_id for log in changed.payload["diagnostics"])


def test_write_failure_does_not_stop_turn_or_recursively_log(runtime_factory, monkeypatch):
    runtime, _ = runtime_factory([response("完成")])
    append = runtime.diagnostics._append
    def fail(*args):
        raise OSError("read-only diagnostics")
    monkeypatch.setattr(runtime.diagnostics, "_append", fail)
    submission = runtime.submit("继续对话")
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert sum(event.type == "diagnostic_error" for event in events) == 1
    assert not any("read-only diagnostics" in log["text"] for log in runtime.diagnostic_records())
    monkeypatch.setattr(runtime.diagnostics, "_append", append)
    submission = runtime.submit("/goal")
    until(runtime, "command_result", submission_id=submission.submission_id)
    assert runtime.session.store.diagnostics_path(runtime.session.session_id).exists()


def test_corrupt_logs_do_not_prevent_resume(runtime_factory):
    runtime, _ = runtime_factory([response("回答")])
    complete(runtime, "你好")
    session_id = runtime.session.session_id
    runtime.close()
    until(runtime, "runtime_closed")
    runtime._thread.join(3)
    path = runtime.session.store.diagnostics_path(session_id)
    path.write_text("broken\n", encoding="utf-8")
    restored, _ = runtime_factory([response("继续回答")])
    submission = restored.submit(f"/resume {session_id}")
    changed, events = until(restored, "session_changed")
    assert changed.payload["records"] and changed.payload["diagnostics"] == []
    assert any(event.type == "diagnostic_error" for event in events)
    until(restored, "command_result", submission_id=submission.submission_id)
    finished, _ = complete(restored, "继续")
    assert finished.payload["outcome"]["status"] == "completed"
    assert path.read_text(encoding="utf-8") == "broken\n"


def test_cli_does_not_duplicate_saved_turn_summary():
    output = []
    adapter = CLIAdapter(None, write=output.append)
    adapter.render(replace(diagnostic_event("session"), payload={
        "text": "[TURN] 完成", "category": "turn_summary"}))
    adapter.render(replace(diagnostic_event("session"), payload={
        "text": "runtime failed", "category": "runtime_error"}))
    assert output == []
    adapter.render(replace(diagnostic_event("session"), type="diagnostic_error",
                           payload={"error": "Cannot save diagnostics"}))
    assert output == ["Cannot save diagnostics"]


def test_deleting_session_cancels_pending_log_retry(runtime_factory, monkeypatch):
    runtime, _ = runtime_factory([response("回答")])
    append = runtime.diagnostics._append
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(runtime.diagnostics, "_append", fail)
    complete(runtime, "第一轮")
    old_id = runtime.session.session_id
    submission = runtime.submit("/new")
    until(runtime, "command_result", submission_id=submission.submission_id)
    submission = runtime.submit(f"/delete {old_id}")
    result, _ = until(runtime, "command_result", submission_id=submission.submission_id)
    assert result.payload["status"] == "completed"
    monkeypatch.setattr(runtime.diagnostics, "_append", append)
    submission = runtime.submit("/goal")
    until(runtime, "command_result", submission_id=submission.submission_id)
    assert not runtime.session.store.path(old_id).exists()
    assert not runtime.session.store.diagnostics_path(old_id).exists()


def test_failed_turn_reason_is_saved_once_with_original_turn(runtime_factory):
    def create(**kwargs):
        raise RuntimeError("model unavailable")
    runtime, _ = runtime_factory(client=create)
    finished, _ = complete(runtime, "你好")
    assert finished.payload["outcome"]["status"] == "failed"
    logs = runtime.diagnostic_records()
    summary = [log for log in logs if "[TURN]" in log["text"]]
    assert len(summary) == 1 and "[TURN] 失败" in summary[0]["text"]
    assert "model unavailable" in summary[0]["text"] and summary[0]["turn_id"] == finished.turn_id


def test_stopped_turn_logs_are_saved_with_original_turn(runtime_factory):
    started, release = threading.Event(), threading.Event()
    def create(**kwargs):
        started.set()
        assert release.wait(3)
        return response("中断回复")
    runtime, _ = runtime_factory(client=create)
    submission = runtime.submit("你好")
    assert started.wait(2)
    begun, _ = until(runtime, "turn_started")
    assert runtime.request_stop(begun.turn_id)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    logs = DiagnosticStore(runtime.session.store.diagnostics_path).read(runtime.session.session_id)
    summary = [log for log in logs if "[TURN]" in log["text"]]
    assert len(summary) == 1 and "[TURN] 已停止" in summary[0]["text"]
    assert summary[0]["turn_id"] == begun.turn_id
