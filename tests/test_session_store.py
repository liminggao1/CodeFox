"""契约测试：会话快照、内容块、goal 恢复和损坏文件处理。"""

from types import SimpleNamespace

import pytest

from agent_harness.agents.goal import GoalController
from agent_harness.runtime.session_store import (InteractiveSession, SessionError,
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
    assert session.record("user_input", "你好", save=False)
    for kind, payload in [("user_message", {"text": "你好"}),
                          ("tool_call", {"id": "call-1", "name": "bash", "input": {"command": "pwd"}}),
                          ("assistant_message", {"text": "完成"})]:
        session.append_record({"id": kind, "at": session.snapshot["created_at"],
                               "kind": "display", "type": kind, "source": "lead",
                               "turn_id": "round", "payload": payload})
    assert session.save()
    loaded = session.store.load(session.session_id)
    assert loaded["messages"][1]["content"][0]["name"] == "bash"
    assert loaded["events"][0]["type"] == "user_input"
    assert reports == [f"[session] {session.session_id}"]
    assert list(session.store.directory.glob("*.tmp")) == []


def test_invalid_target_is_listed_as_deletable_and_rejected_by_load(tmp_path):
    store = SessionStore(tmp_path)
    store.directory.mkdir(parents=True)
    bad = store.directory / "20261009-000000-deadbeef.json"
    bad.write_text("{not json", encoding="utf-8")
    entries, errors = store.list_sessions()
    # 坏文件列进列表（标记 invalid）方便删除，但读取仍然报错。
    assert [entry["id"] for entry in entries] == ["20261009-000000-deadbeef"]
    assert entries[0]["invalid"] and errors
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


def test_session_input_measurement_is_independent_of_history_and_can_be_unknown(tmp_path):
    session, history, _, _ = make_session(tmp_path)
    assert session.last_input_tokens() == 0
    session.record("user_input", "你好", save=False)
    history.append({"role": "user", "content": "你好"})
    assert session.last_input_tokens() is None
    session.append_record({"id": "user", "at": session.snapshot["created_at"],
                           "kind": "display", "type": "user_message", "source": "lead",
                           "turn_id": "turn", "payload": {"text": "你好"}})
    session.record_input_tokens(2100)
    session.record_input_tokens(3200)
    assert session.last_input_tokens() == 3200
    assert history == [{"role": "user", "content": "你好"}]
    assert session.save()
    restored, _, _, _ = make_session(tmp_path)
    restored.activate(session.store.load(session.session_id))
    assert restored.last_input_tokens() == 3200


@pytest.mark.parametrize("value", [-1, True, "3200", None])
def test_invalid_input_measurement_is_rejected_without_rewriting_file(tmp_path, value):
    session, _, _, _ = make_session(tmp_path)
    session.record("goal_query")
    snapshot = session.store.load(session.session_id)
    snapshot["events"].append({"id": "bad-usage", "at": snapshot["created_at"],
                               "kind": "summary", "type": "model_usage", "input_tokens": value})
    session.store.save(snapshot)
    original = session.store.path(session.session_id).read_bytes()
    with pytest.raises(SessionError, match="input token measurement"):
        session.store.load(session.session_id)
    assert session.store.path(session.session_id).read_bytes() == original
