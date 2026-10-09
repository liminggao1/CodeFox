"""契约测试：会话快照、内容块、goal 恢复和损坏文件处理。"""

from types import SimpleNamespace

import pytest

from agent_harness.goal import GoalController
from agent_harness.session_store import (InteractiveSession, SessionError,
                                          SessionStore)


class Goal:
    def __init__(self):
        self.value = GoalController(evaluator=None)

    def snapshot(self):
        return self.value.snapshot()


def make_session(tmp_path):
    goal = Goal()
    history = []
    reports = []
    session = InteractiveSession(SessionStore(tmp_path), history,
                                 lambda: goal.value, reports.append)
    return session, history, reports, goal


def test_first_event_creates_one_atomic_snapshot_and_round_trips_blocks(tmp_path):
    session, history, reports, _ = make_session(tmp_path)
    history.append({"role": "user", "content": "你好"})
    history.append({"role": "assistant", "content": [
        SimpleNamespace(type="tool_use", id="call-1", name="bash", input={"command": "pwd"}),
        SimpleNamespace(type="text", text="完成"),
    ]})
    assert session.record("user_input", "你好")
    loaded = session.store.load(session.session_id)
    assert loaded["messages"][1]["content"][0]["name"] == "bash"
    assert loaded["events"][0]["type"] == "user_input"
    assert reports == [f"[session] {session.session_id}"]
    assert list(session.store.directory.glob("*.tmp")) == []


def test_invalid_target_does_not_appear_in_listing_or_load(tmp_path):
    store = SessionStore(tmp_path)
    store.directory.mkdir(parents=True)
    bad = store.directory / "20261009-000000-deadbeef.json"
    bad.write_text("{not json", encoding="utf-8")
    entries, errors = store.list_sessions()
    assert entries == [] and errors
    with pytest.raises(SessionError):
        store.load("20261009-000000-deadbeef")


def test_goal_snapshot_restores_iterations_and_last_status():
    controller = GoalController(evaluator=None, block_cap=3)
    controller.set_goal("ship it")
    controller.active.iterations = 4
    controller.active.last_reason = "missing evidence"
    controller.consecutive_blocks = 2
    snapshot = controller.snapshot()
    restored = GoalController.from_snapshot(None, snapshot, block_cap=3)
    assert restored.active.condition == "ship it"
    assert restored.active.iterations == 4
    assert restored.active.last_reason == "missing evidence"
    assert restored.consecutive_blocks == 2


def test_save_failure_keeps_dirty_context_for_retry(tmp_path, monkeypatch):
    session, history, _, _ = make_session(tmp_path)
    history.append({"role": "user", "content": "保存"})
    real_save = session.store.save
    monkeypatch.setattr(session.store, "save", lambda snapshot: (_ for _ in ()).throw(OSError("full")))
    assert not session.record("user_input", "保存")
    assert session.dirty
    monkeypatch.setattr(session.store, "save", real_save)
    assert session.save()
    assert not session.dirty


@pytest.mark.parametrize("field,value", [
    ("workdir", "relative"), ("task_id", "unknown"),
    ("worktree", "../outside"), ("todos", [{"content": "verify", "status": "unknown"}]),
])
def test_invalid_context_fields_are_rejected(tmp_path, field, value):
    session, _, _, _ = make_session(tmp_path)
    session.record("goal_query")
    saved = session.store.load(session.session_id)
    saved[field] = value
    session.store.save(saved)
    with pytest.raises(SessionError):
        session.store.load(session.session_id)
