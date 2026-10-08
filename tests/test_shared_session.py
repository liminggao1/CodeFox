"""Shared child execution, context recovery and tool side effects."""

from types import SimpleNamespace

import pytest

from agent_harness.subagent_session import SubagentSession


def response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


@pytest.mark.parametrize("mode", ["once", "persistent"])
def test_truncated_tool_never_writes_or_appends(host_factory, mode):
    host = host_factory()
    block = SimpleNamespace(type="tool_use", id="write", name="write_file",
                            input={"path": "partial.txt", "content": "partial"})
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: response(block, stop_reason="max_tokens")))
    session = SubagentSession("child", client, "test", "", mode=mode)
    messages = [{"role": "user", "content": "Write"}]
    with pytest.raises(RuntimeError, match="truncated"):
        session.run(messages)
    assert len(messages) == 1
    assert not (host.core.WORKDIR / "partial.txt").exists()


@pytest.mark.parametrize("mode", ["once", "persistent"])
def test_unassigned_workspace_uses_startup_directory(host_factory, mode):
    host = host_factory()
    session = SubagentSession("child", None, "test", "", mode=mode)
    result = session.execute_tool(SimpleNamespace(
        name="write_file", input={"path": "result.txt", "content": "done"}))
    assert "Wrote" in result
    assert (host.core.WORKDIR / "result.txt").read_text() == "done"


def test_child_compaction_uses_injected_client_and_keeps_recent_tool_pair(
    host_factory, monkeypatch,
):
    host = host_factory()
    monkeypatch.setattr(host.core, "COMPACT_TRIGGER", 30)
    monkeypatch.setattr(host.core, "COMPACT_TARGET", 20)
    monkeypatch.setattr(host.core, "COMPACTION_TAIL_MESSAGES", 1)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return response(text_block("facts"))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    messages = [{"role": "user", "content": "Old completed task " * 10},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "read",
                    "name": "read_file", "input": {"path": "file.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result",
                    "tool_use_id": "read", "content": "recent evidence"}]}]
    identity = id(messages)
    result = host.compaction.prepare_history(
        messages, "New delegation B", client=client, model="child-model",
        preserve_tail=True)
    assert result.applied and result.reason is None
    assert id(messages) == identity
    assert calls[0]["model"] == "child-model"
    assert "New delegation B" in messages[0]["content"]
    assert messages[-2]["content"][0]["id"] == "read"
    assert messages[-1]["content"][0]["tool_use_id"] == "read"


def test_context_recovery_is_once_per_delegation(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setattr(host.core, "COMPACTION_TAIL_MESSAGES", 1)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if "tools" not in kwargs:
            return response(text_block("Earlier findings"))
        if len(calls) in {1, 4}:
            raise ValueError("context_length_exceeded")
        return response(text_block("Done"))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    session = SubagentSession("child", client, "test", "", mode="once")
    session.begin_work("Investigate B")
    messages = [{"role": "user", "content": "Earlier A"},
                {"role": "assistant", "content": [text_block("A finished")]},
                {"role": "user", "content": "Investigate B"}]
    session.run(messages)
    with pytest.raises(ValueError, match="context_length"):
        session.run(messages)
    assert len(calls) == 4


def test_failed_reactive_summary_keeps_history(host_factory):
    host_factory()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            raise ValueError("context_length_exceeded")
        raise RuntimeError("summary failed")
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    session = SubagentSession("child", client, "test", "", mode="once")
    messages = [{"role": "user", "content": "Current delegation"}]
    before = list(messages)
    with pytest.raises(RuntimeError, match="recovery failed"):
        session.run(messages)
    assert messages == before
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["once", "persistent"])
def test_current_delegation_survives_middle_history_archive(host_factory, mode):
    host = host_factory()
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response(text_block("Current work complete"))

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    session = SubagentSession("child", client, "test", "Child rules", mode=mode)
    messages = [{"role": "user", "content": "Old delegation A"}]
    messages.extend({"role": "assistant" if i % 2 == 0 else "user",
                     "content": f"Short progress {i}"} for i in range(79))
    messages[20] = {"role": "user", "content": "Current delegation B"}
    session.begin_work("Current delegation B")

    session.run(messages)

    assert len(calls) == 1  # Archiving short messages does not request a summary.
    assert "archived at" in str(calls[0]["messages"])
    assert "Current delegation B" not in str(calls[0]["messages"])
    assert "Current delegation B" in calls[0]["system"]
    assert "Old delegation A" not in calls[0]["system"]
    assert "No assigned Task" in calls[0]["system"]
    assert list((host.core.WORKDIR / ".transcripts").glob("*.jsonl"))
