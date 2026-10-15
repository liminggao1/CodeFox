"""契约测试：会话快照、内容块、goal 恢复和损坏文件处理。"""

from types import SimpleNamespace

import pytest

from codefox.agents.goal import GoalController
from codefox.runtime.session_store import (InteractiveSession, SessionError,
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


def test_delete_removes_session_artifacts_with_session_files(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = store.create()
    store.save(snapshot)
    other = store.create()
    store.save(other)
    store.diagnostics_path(other["id"]).write_text("keep\n", encoding="utf-8")
    store.diagnostics_path(snapshot["id"]).write_text("diagnostic\n", encoding="utf-8")
    artifact = store.artifacts_path(snapshot["id"]) / "transcripts" / "transcript.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")

    store.delete(snapshot["id"])

    assert not store.path(snapshot["id"]).exists()
    assert not store.diagnostics_path(snapshot["id"]).exists()
    assert not store.artifacts_path(snapshot["id"]).exists()
    assert store.path(other["id"]).exists()
    assert store.diagnostics_path(other["id"]).read_text(encoding="utf-8") == "keep\n"


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


def test_invalid_context_fields_are_rejected(tmp_path):
    for field, value in [
        ("workdir", "relative"),
        ("task_id", "unknown"),
        ("worktree", "../outside"),
        ("todos", [{"content": "verify", "status": "unknown"}]),
    ]:
        session, _, _, _ = make_session(tmp_path)
        session.record("goal_query")
        saved = session.store.load(session.session_id)
        saved[field] = value
        session.store.save(saved)
        with pytest.raises(SessionError):
            session.store.load(session.session_id)


def test_invalid_input_measurement_is_rejected_without_rewriting_file(tmp_path):
    for value in (-1, True, "3200", None):
        session, _, _, _ = make_session(tmp_path)
        session.record("goal_query")
        snapshot = session.store.load(session.session_id)
        snapshot["events"].append({"id": "bad-usage", "at": snapshot["created_at"],
                                   "kind": "summary", "type": "model_usage", "input_tokens": value})
        session.store.save(snapshot)
        original = session.store.path(session.session_id).read_bytes()
        with pytest.raises(SessionError, match="input token measurement"):
            session.store.load(session.session_id)
        assert session.store.path(session.session_id).read_bytes() == original, value
