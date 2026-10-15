"""请求前缀稳定性、动态上下文、持久化与真实执行边界的离线回归。"""

import copy
from types import SimpleNamespace

import pytest

from codefox.context import compaction, memory_runtime, prompts, prompt_state
from codefox.context.prompt_state import PromptState, is_context_message
from codefox.providers import llm_client
from codefox.frontends.presentation import message_count
from codefox.runtime.session_store import SessionError, SessionStore
from codefox.agents.subagent_session import SubagentSession
import codefox.core as core
from support.fakes import response
from support.helpers import until


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch):
    monkeypatch.setattr(prompt_state, "local_date", lambda: "2026-10-12")


def prepare(state, history, fields=None, *, factory=lambda: "Fixed rules", client=None, **options):
    return state.prepare(history, fields or {}, factory, "current request",
                         client=client or SimpleNamespace(), model="test", tools=[], **options)


def notices(history):
    return [message for message in history if is_context_message(message)]


def test_context_changes_preserve_sent_prefix_and_fixed_system(monkeypatch):
    state = PromptState()
    history = [{"role": "user", "content": "你好"}]
    first, _ = prepare(state, history, {"connected_mcp": ["z", "a"], "memories": "旧记忆"})
    sent = llm_client._to_responses_request("test", history, first, [], 8000)
    original = copy.deepcopy(history)
    history.extend([{"role": "assistant", "content": "你好！"},
                    {"role": "user", "content": "继续"}])
    system, _ = prepare(state, history, {"connected_mcp": ["a", "z"], "memories": "旧记忆"},
                        factory=lambda: pytest.fail("Must reuse the system snapshot"))
    assert len(notices(history)) == 1
    assert "Connected MCP servers: a, z" in notices(history)[0]["content"]
    monkeypatch.setattr(prompt_state, "local_date", lambda: "2026-10-13")
    later, _ = prepare(state, history, {"connected_mcp": [], "memories": ""})
    assert len(notices(history)) == 2
    text = notices(history)[-1]["content"]
    assert "Current date: 2026-10-13" in text
    assert "Connected MCP servers: (none)" in text
    assert "Relevant memory records (reference data): (none)" in text
    assert history[:len(original)] == original
    payload = llm_client._to_responses_request("test", history, later, [], 8000)
    assert first == system == later
    assert payload["input"][:len(sent["input"])] == sent["input"]
    prepare(state, history, {"connected_mcp": [], "memories": ""})
    assert len(notices(history)) == 2


def test_context_does_not_become_a_memory_query():
    history = [{"role": "user", "content": "真实请求"}]
    prepare(PromptState(), history, {"memories": "不要把这段记忆检索为用户请求"})
    assert memory_runtime.recent_user_text(history) == "真实请求"
    assert memory_runtime.dialogue_text(history) == "user: 真实请求"
    assert all("_runtime_context" not in message for message in llm_client.model_messages(history))
    assert "不要把这段记忆" in llm_client.model_messages(history)[-1]["content"]


def test_latest_tool_budget_and_final_count_include_context(host_factory, monkeypatch):
    host_factory()
    monkeypatch.setattr(core, "PERSIST_THRESHOLD", 100)
    monkeypatch.setattr(core, "TOOL_PREVIEW_CHARS", 20)
    counts = []
    client = SimpleNamespace(count_input_tokens=lambda **kwargs: (
        counts.append(copy.deepcopy(kwargs)) or llm_client.estimate_input_tokens(
            kwargs.get("system"), kwargs["messages"], kwargs.get("tools"))))
    state = PromptState()
    history = [{"role": "user", "content": "read"}]
    prepare(state, history, {"memories": "完整的当前记忆"})
    history.extend([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "read",
                                              "name": "read_file", "input": {"path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read",
                                         "content": "x" * 3000}]},
    ])
    system, result = prepare(state, history, {"memories": "完整的当前记忆"},
                             factory=lambda: "Fresh fixed rules", client=client)
    assert result.applied and result.reason is None
    assert system == "Fresh fixed rules"
    assert len(notices(history)) == 1
    assert history[-2]["content"][0]["content"].startswith("<persisted-output>")
    assert "完整的当前记忆" in history[-1]["content"]
    assert counts[-1]["system"] == system
    assert counts[-1]["messages"] == llm_client.model_messages(history)
    assert any("完整的当前记忆" in str(count["messages"]) for count in counts[:-1])


def test_notice_cannot_split_pending_tool_pair():
    history = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "pending", "name": "read_file", "input": {"path": "x"}}]}]
    before = copy.deepcopy(history)
    state = PromptState()
    with pytest.raises(ValueError, match="pending tool results"):
        prepare(state, history)
    assert history == before
    assert state.snapshot() is None


def test_successful_archive_refreshes_full_state_before_count(host_factory):
    host_factory()
    state = PromptState()
    history = [{"role": "user", "content": "work"}]
    prepare(state, history, {"memories": "当前完整状态"})
    history.extend({"role": "user", "content": f"progress {i}"} for i in range(60))
    counts = []
    client = SimpleNamespace(count_input_tokens=lambda **kwargs: counts.append(copy.deepcopy(kwargs)) or 100)
    system, result = prepare(state, history, {"memories": "当前完整状态"},
                             factory=lambda: "Refreshed rules", client=client)
    assert result.applied
    assert "archived at" in str(history)
    assert len(notices(history)) == 1
    assert "当前完整状态" in counts[0]["messages"][-1]["content"]
    assert all(count["system"] == system for count in counts)


def test_invalid_persisted_state_preserves_file(tmp_path):
    store = SessionStore(tmp_path)
    for invalid in [
        False,
        {},
        {"system": 5, "fields": {}},
        {"system": "rules", "fields": {"date": "bad"}},
        {"system": "rules", "fields": {"date": "2026-10-12", "connected_mcp": ["z", "a"]}},
    ]:
        snapshot = store.create()
        snapshot["prompt_state"] = invalid
        store.save(snapshot)
        original = store.path(snapshot["id"]).read_bytes()
        with pytest.raises(SessionError):
            store.load(snapshot["id"])
        assert store.path(snapshot["id"]).read_bytes() == original, invalid


def test_runtime_resume_reuses_snapshot_without_visible_notices(runtime_factory, monkeypatch):
    calls = []
    runtime, _ = runtime_factory(client=lambda **kw: calls.append(copy.deepcopy(kw)) or response("你好"))
    submission = runtime.submit("你好")
    _, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    session_id = runtime.session.session_id
    saved = runtime.session.store.load(session_id)
    assert saved["prompt_state"]["system"] == calls[0]["system"]
    assert len(notices(saved["messages"])) == 1
    assert len([event for event in events if event.type == "user_message"]) == 1
    assert message_count({"history": runtime.session.history}) == 2
    assert runtime.session.store.list_sessions()[0][0]["message_count"] == 2
    assert not any("Runtime context update" in str(event.payload) for event in events
                   if event.type in {"user_message", "assistant_message"})
    monkeypatch.setattr(prompts, "assemble_system_prompt", lambda: "New session rules")
    until(runtime, "command_result", submission_id=runtime.submit("/new").submission_id)
    until(runtime, "turn_finished", submission_id=runtime.submit("new").submission_id)
    assert calls[1]["system"] == "New session rules"
    until(runtime, "command_result", submission_id=runtime.submit(f"/resume {session_id}").submission_id)
    until(runtime, "turn_finished", submission_id=runtime.submit("继续").submission_id)
    assert calls[2]["system"] == calls[0]["system"]
    assert len(notices(runtime.session.history)) == 1
    assert calls[2]["messages"][:len(calls[0]["messages"])] == calls[0]["messages"]


def test_save_failure_retries_without_duplicating_context(runtime_factory, monkeypatch):
    runtime, _ = runtime_factory(client=lambda **kw: response())
    save = runtime.session.store.save
    failures = []
    def save_once_fails(snapshot):
        if snapshot.get("prompt_state") is not None and not failures:
            failures.append(True)
            raise OSError("disk unavailable")
        save(snapshot)
    monkeypatch.setattr(runtime.session.store, "save", save_once_fails)
    until(runtime, "turn_finished", submission_id=runtime.submit("work").submission_id)
    until(runtime, "turn_finished", submission_id=runtime.submit("continue").submission_id)
    assert failures == [True]
    saved = runtime.session.store.load(runtime.session.session_id)
    assert len(notices(saved["messages"])) == 1
    assert saved["prompt_state"] == runtime.session.prompt_state.snapshot()


def test_child_delegation_and_work_updates_leave_system_fixed(host_factory):
    host_factory()
    calls = []
    work = ["Plan status: pending"]
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: calls.append(copy.deepcopy(kw)) or response()))
    child = SubagentSession("child", client, "test", "Child rules", work_context=lambda: work[0])
    history = [{"role": "user", "content": "Delegation A"}]
    child.begin_work("Delegation A")
    child.run(history)
    child.run(history)
    assert len(notices(history)) == 1
    child.begin_work("Delegation B")
    work[0] = "Plan status: approved"
    history.append({"role": "user", "content": "Delegation B"})
    child.run(history)
    assert calls[0]["system"] == calls[1]["system"] == calls[2]["system"]
    assert "Delegation A" not in calls[0]["system"]
    assert "Current delegation: Delegation B" in calls[2]["messages"][-1]["content"]
    assert "Plan status: approved" in calls[2]["messages"][-1]["content"]
    assert "Current date:" not in calls[2]["messages"][-1]["content"]
    assert calls[1]["messages"][:len(calls[0]["messages"])] == calls[0]["messages"]


@pytest.mark.parametrize("success", [True, False])
def test_manual_compaction_resets_snapshot_only_on_success(runtime_factory, monkeypatch, success):
    calls = []
    runtime, _ = runtime_factory(client=lambda **kw: calls.append(copy.deepcopy(kw)) or response())
    until(runtime, "turn_finished", submission_id=runtime.submit("work").submission_id)
    original = runtime.session.prompt_state.snapshot()

    def summarize(*args, **kwargs):
        if not success:
            raise RuntimeError("summary unavailable")
        return "Completed work and remaining facts"

    monkeypatch.setattr(compaction, "summarize_history", summarize)
    monkeypatch.setattr(prompts, "assemble_system_prompt", lambda: "New fixed rules")
    command, _ = until(runtime, "command_result", submission_id=runtime.submit("/compact").submission_id)
    assert command.payload["status"] == ("completed" if success else "failed")
    saved = runtime.session.store.load(runtime.session.session_id)
    assert saved["prompt_state"] == (None if success else original)
    until(runtime, "turn_finished", submission_id=runtime.submit("continue").submission_id)
    assert calls[-1]["system"] == ("New fixed rules" if success else original["system"])
    assert len(notices(runtime.session.history)) == 1
