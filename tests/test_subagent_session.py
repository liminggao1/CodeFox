"""子 agent 会话：持久执行、共享执行、上下文恢复与工具副作用。"""

from types import SimpleNamespace
import os

import pytest

from agent_harness.agents.subagent_session import SubagentBudgetExceeded, SubagentSession
from agent_harness.context.prompt_state import is_context_message
from support.fakes import blocks, text_block, tool_call
import agent_harness.agents.subagent as subagent_runtime
import agent_harness.context.compaction as compaction
import agent_harness.core as core
import agent_harness.workspace.tasks as tasks
from agent_harness.providers.llm_client import APIConnectionError


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
    subagent_runtime.spawn_subagent("alice", "Finish the task", "persistent",
                                     description="Finish the task", role="tester", task_id=task.id)

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


def test_child_tool_budget_pairs_unexecuted_calls_and_stops_without_summary(
    host_factory, no_approval,
):
    host = host_factory()
    calls = []
    first = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="tool_use", id="one", name="read_file",
                            input={"path": "missing-one.txt"}),
            SimpleNamespace(type="tool_use", id="two", name="read_file",
                            input={"path": "missing-two.txt"}),
        ],
    )
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: calls.append(kwargs) or first))
    session = SubagentSession("child", client, "test", "", mode="once",
                              tools=["read_file"], max_tool_calls=1)
    messages = [{"role": "user", "content": "Inspect"}]

    with pytest.raises(SubagentBudgetExceeded, match="tool-call budget exhausted"):
        session.run(messages)

    assert len(calls) == 1
    results = messages[-1]["content"]
    assert [result["tool_use_id"] for result in results] == ["one", "two"]
    assert results[0]["is_error"] is True
    assert results[1]["is_error"] is True
    assert "Stopped: subagent tool-call budget exhausted" in results[1]["content"]


def test_child_network_failure_does_not_consume_model_budget(host_factory):
    host = host_factory()
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: (_ for _ in ()).throw(APIConnectionError("offline"))))
    result = subagent_runtime.spawn_subagent(
        "offline-child", "Inspect", "once", client=client,
        description="Inspect offline", max_rounds=1,
    )

    assert result["status"] == "network_error"
    assert result["usage"]["model_calls"] == 0
    assert "offline" in result["error"]


@pytest.mark.parametrize("recovery", ["automatic", "reactive"])
def test_summary_network_failure_remains_a_network_error(host_factory, monkeypatch, recovery):
    host_factory()
    if recovery == "automatic":
        monkeypatch.setattr(core, "COMPACT_TRIGGER", 30)
        monkeypatch.setattr(core, "COMPACT_TARGET", 20)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            raise ValueError("context_length_exceeded")
        raise APIConnectionError("summary endpoint offline")

    result = subagent_runtime.spawn_subagent(
        "summary-offline", "Inspect source " * 30, "once", description="Inspect source",
        tools=["read_file"], max_rounds=3,
        client=SimpleNamespace(messages=SimpleNamespace(create=create)))

    assert result["status"] == "network_error"
    assert "summary endpoint offline" in result["error"]
    assert result["rounds"] == result["usage"]["model_calls"] == (recovery == "reactive")
    assert len(calls) == (2 if recovery == "reactive" else 1)
    assert result["tool_calls"] == 0
    assert subagent_runtime.get_subagent_output(result["agent_id"], block=False) == result


def test_child_session_archive_path_is_not_read_through_generic_file_tool(
    host_factory, tmp_path, no_approval,
):
    host = host_factory()
    archive = core.sessions_dir() / "20261014-000000-abcdef12.artifacts" / "tool-results" / "x.txt"
    archive.parent.mkdir(parents=True)
    archive.write_text("internal result", encoding="utf-8")
    session = SubagentSession("child", None, "test", "", mode="once",
                              tools=["read_file"])

    result = session.execute_tool(SimpleNamespace(
        name="read_file", input={"path": str(archive)})).result

    assert result.status == "failed"
    assert "session tool-result archives" in result.content


def test_relative_session_archive_is_blocked_from_the_assigned_directory(
    host_factory, monkeypatch,
):
    host_factory()
    from agent_harness.runtime import hooks
    from agent_harness.workspace import worktrees

    directory = core.WORKDIR / ".worktrees" / "reader"
    directory.mkdir(parents=True)
    monkeypatch.setattr(worktrees, "_registered_worktree", lambda name: (directory, None))
    task = tasks.create_task("Inspect")
    task.worktree = "reader"
    tasks.save_task(task)
    tasks.claim_task(task.id, owner="reader")
    archive = core.sessions_dir() / "20261014-000000-abcdef12.artifacts" / "tool-results" / "x.txt"
    archive.parent.mkdir(parents=True)
    archive.write_text("internal result", encoding="utf-8")
    monkeypatch.setattr(hooks, "check_tool_use",
                        lambda *args, **kwargs: pytest.fail("archive must bypass path approval"))
    session = SubagentSession("reader", None, "test", "", tools=["read_file"])
    result = session.execute_tool(SimpleNamespace(
        name="read_file", input={"path": os.path.relpath(archive, directory)})).result
    assert result.status == "failed"
    assert "session tool-result archives" in result.content
    assert "offset" in result.content and "limit" in result.content


def test_child_pre_tool_failure_pairs_the_entire_batch(host_factory, monkeypatch):
    host_factory()
    from agent_harness.runtime import hooks

    def fail(block):
        raise RuntimeError("pre-tool hook failed")

    monkeypatch.setitem(hooks.HOOKS, "PreToolUse", [fail])
    response = blocks(*[SimpleNamespace(
        type="tool_use", id=call_id, name="read_file", input={"path": "file.txt"})
        for call_id in ("first", "second")])
    session = SubagentSession(
        "reader", SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response)),
        "test", "", mode="once", tools=["read_file"])
    messages = [{"role": "user", "content": "Inspect"}]
    with pytest.raises(RuntimeError, match="pre-tool hook failed"):
        session.run(messages)
    results = messages[-1]["content"]
    assert [item["tool_use_id"] for item in results] == ["first", "second"]
    assert all(item["is_error"] for item in results)
    assert "pre-tool hook failed" in results[0]["content"]
    assert "Interrupted" in results[1]["content"]
    assert sum(isinstance(message["content"], list) and message["role"] == "user"
               for message in messages) == 1


def test_context_summary_cannot_spend_an_extra_model_request(host_factory, monkeypatch):
    host_factory()
    monkeypatch.setattr(core, "COMPACT_TRIGGER", 30)
    monkeypatch.setattr(core, "COMPACT_TARGET", 20)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return blocks(text_block("Earlier findings"))
    result = subagent_runtime.spawn_subagent(
        "bounded-summary", "Inspect source " * 30, "once", description="Inspect source",
        client=SimpleNamespace(messages=SimpleNamespace(create=create)), max_rounds=1)
    assert len(calls) == 1
    assert "tools" not in calls[0]
    assert result["status"] == "budget_exhausted"
    assert result["rounds"] == 1 and result["tool_calls"] == 0


@pytest.mark.parametrize("earlier_text", ["", "Earlier plan"])
def test_budget_output_preserves_the_latest_tool_evidence(host_factory, no_approval, earlier_text):
    host_factory()
    core.WORKDIR.joinpath("old.txt").write_text("Old evidence", encoding="utf-8")
    core.WORKDIR.joinpath("new.txt").write_text("Current evidence", encoding="utf-8")
    responses = iter([
        blocks(*([text_block(earlier_text)] if earlier_text else []),
               tool_call("read_file", path="old.txt")),
        blocks(tool_call("read_file", path="new.txt")),
    ])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return next(responses)

    result = subagent_runtime.spawn_subagent(
        "reader", "Read both files", "once", description="Inspect current evidence",
        tools=["read_file"], max_rounds=2,
        client=SimpleNamespace(messages=SimpleNamespace(create=create)))

    assert result["status"] == "budget_exhausted"
    assert "Current evidence" in result["text"]
    assert "Old evidence" not in result["text"] and "Earlier plan" not in result["text"]
    assert result["rounds"] == 2 and result["tool_calls"] == 2
    assert len(calls) == 2
    assert subagent_runtime.get_subagent_output(result["agent_id"], block=False) == result


def test_sessions_keep_configuration_history_and_work_budgets_independent(host_factory):
    host_factory()
    calls = {"first": [], "second": []}
    sessions, histories = [], []
    for name, tools in (("first", ["read_file"]), ("second", ["glob"])):
        def create(name=name, **kwargs):
            calls[name].append(kwargs)
            return blocks(text_block(f"{name} finding"))
        session = SubagentSession(name, SimpleNamespace(messages=SimpleNamespace(create=create)),
                                  f"{name}-model", f"{name} rules", mode="persistent", tools=tools)
        history = [{"role": "user", "content": f"{name} delegation"}]
        session.begin_work(f"{name} delegation")
        session.run(history)
        sessions.append(session)
        histories.append(history)

    first, second = sessions
    second_state = second.prompt_state.snapshot()
    second_history = list(histories[1])
    first.begin_work("First next delegation")
    histories[0].append({"role": "user", "content": "First next delegation"})
    first.run(histories[0])

    assert first.usage["model_calls"] == 2 and second.usage["model_calls"] == 1
    assert first.rounds == second.rounds == 1
    assert first.prompt_state is not second.prompt_state and first.engine is not second.engine
    assert second.prompt_state.snapshot() == second_state and histories[1] == second_history
    for name, tools in (("first", {"read_file"}), ("second", {"glob"})):
        assert all(item["model"] == f"{name}-model" for item in calls[name])
        assert all({tool["name"] for tool in item["tools"]} == tools for item in calls[name])
        assert all(f"{name} rules" in item["system"] for item in calls[name])
    assert "First next delegation" not in str(histories[1])


def test_child_snapshot_detaches_model_mutations_from_history_and_tools(host_factory):
    host_factory()
    def create(**kwargs):
        kwargs["messages"][0]["content"] = "Changed by client"
        kwargs["tools"][0]["description"] = "Changed by client"
        return blocks(text_block("done"))
    child = SubagentSession("child", SimpleNamespace(messages=SimpleNamespace(create=create)),
                            "test", "Child rules", tools=["read_file"])
    history = [{"role": "user", "content": "Inspect original"}]
    child.run(history)
    assert history[0]["content"] == "Inspect original"
    assert child.tools[0]["description"] != "Changed by client"


def test_child_context_changes_follow_complete_tool_results(host_factory):
    host_factory()
    calls = []
    values = iter([blocks(tool_call("read_file", path="missing.txt")), blocks(text_block("done"))])
    work = ["Plan status: required"]
    child = SubagentSession("child", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: calls.append(kwargs) or next(values))), "test", "Child rules",
        mode="persistent", tools=["read_file"], work_context=lambda: work[0])
    child.begin_work("Inspect")

    def coordinate():
        if child.rounds:
            work[0] = "Plan status: approved"

    child.run([{"role": "user", "content": "Inspect"}], before_iteration=coordinate)
    assert len(calls) == 2 and calls[0]["system"] == calls[1]["system"]
    messages = calls[1]["messages"]
    assert messages[-2]["content"][0]["type"] == "tool_result"
    assert "Plan status: approved" in messages[-1]["content"]
    assert "Current delegation:" not in messages[-1]["content"]
