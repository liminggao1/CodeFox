"""子 agent 会话：持久执行、共享执行、上下文恢复与工具副作用。"""

from types import SimpleNamespace

import pytest

from agent_harness.agents.subagent_session import SubagentSession
from agent_harness.context.prompt_state import is_context_message
from support.fakes import blocks, text_block
import agent_harness.agents.subagent as subagent_runtime
import agent_harness.context.compaction as compaction
import agent_harness.core as core
import agent_harness.workspace.tasks as tasks


@pytest.mark.parametrize("fail", [False, True, "truncated_text", "truncated_tool"])
def test_subagent_completion_and_failure_release_assignment(host_factory, monkeypatch, fail):
    host = host_factory()
    task = tasks.create_task("Session task")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if fail is True:
            raise RuntimeError("model failed")
        if fail == "truncated_text":
            return SimpleNamespace(stop_reason="max_tokens", content=[
                SimpleNamespace(type="text", text="Partial result")])
        if fail == "truncated_tool":
            return SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(
                type="tool_use", id="finish", name="complete_task", input={"task_id": task.id}
            )])
        if len(calls) == 1:
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use", id="finish", name="complete_task", input={"task_id": task.id}
            )])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Finished")])

    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(subagent_runtime.threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))

    def shutdown(name, timeout=None):
        subagent_runtime.run_request_shutdown(name).content
        return subagent_runtime._BUS.read_inbox(name)

    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", shutdown)
    subagent_runtime.spawn_subagent("alice", "Finish the task", "persistent", role="tester", task_id=task.id)

    assert "alice" not in tasks.assignments
    assert "alice" not in subagent_runtime._active_subagents
    assert tasks.load_task(task.id).status == ("pending" if fail else "completed")
    inbox = subagent_runtime._BUS.read_inbox("lead")
    assert any(msg["type"] == ("error" if fail else "result") for msg in inbox)
    if fail:
        assert not any(msg["type"] == "result" for msg in inbox)
        assert len(calls[0]["messages"]) == 2
        assert "Runtime context update" in calls[0]["messages"][1]["content"]
    assert "[Assigned task" in calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("mode", ["once", "persistent"])
def test_truncated_tool_never_writes_or_appends(host_factory, mode):
    host = host_factory()
    block = SimpleNamespace(type="tool_use", id="write", name="write_file",
                            input={"path": "partial.txt", "content": "partial"})
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: blocks(block, stop_reason="max_tokens")))
    session = SubagentSession("child", client, "test", "", mode=mode)
    messages = [{"role": "user", "content": "Write"}]
    with pytest.raises(RuntimeError, match="truncated"):
        session.run(messages)
    assert [m for m in messages if not is_context_message(m)] == [{"role": "user", "content": "Write"}]
    assert not (core.WORKDIR / "partial.txt").exists()


@pytest.mark.parametrize("mode", ["once", "persistent"])
def test_unassigned_workspace_uses_startup_directory(host_factory, mode, no_approval):
    host = host_factory()
    session = SubagentSession("child", None, "test", "", mode=mode)
    result = session.execute_tool(SimpleNamespace(
        name="write_file", input={"path": "result.txt", "content": "done"})).result.content
    assert "Wrote" in result
    assert (core.WORKDIR / "result.txt").read_text() == "done"


def test_child_compaction_uses_injected_client_and_keeps_recent_tool_pair(
    host_factory, monkeypatch,
):
    host = host_factory()
    monkeypatch.setattr(core, "COMPACT_TRIGGER", 30)
    monkeypatch.setattr(core, "COMPACT_TARGET", 20)
    monkeypatch.setattr(core, "COMPACTION_TAIL_MESSAGES", 1)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return blocks(text_block("facts"))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    messages = [{"role": "user", "content": "Old completed task " * 10},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "read",
                    "name": "read_file", "input": {"path": "file.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result",
                    "tool_use_id": "read", "content": "recent evidence"}]}]
    identity = id(messages)
    result = compaction.prepare_history(
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
    monkeypatch.setattr(core, "COMPACTION_TAIL_MESSAGES", 1)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if "tools" not in kwargs:
            return blocks(text_block("Earlier findings"))
        if len(calls) in {1, 4}:
            raise ValueError("context_length_exceeded")
        return blocks(text_block("Done"))
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
    assert [m for m in messages if not is_context_message(m)] == before
    assert session.prompt_state.snapshot() is not None
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["once", "persistent"])
def test_current_delegation_survives_middle_history_archive(host_factory, mode):
    host = host_factory()
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return blocks(text_block("Current work complete"))

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
    assert "Current delegation B" in str(calls[0]["messages"])
    assert "Current delegation B" not in calls[0]["system"]
    assert "Old delegation A" not in calls[0]["system"]
    assert "No assigned Task" in str(calls[0]["messages"])
    assert list((core.WORKDIR / ".transcripts").glob("*.jsonl"))
