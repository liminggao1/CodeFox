"""通过真实工具观察执行状态与正文的独立性。"""

from agent_harness.tools import file_tools, shared_tools
from agent_harness.workspace import tasks
from agent_harness.runtime import hooks
from test_interactive_runtime import runtime_factory, response, tool, until
from test_conversation_records import stored, records


def test_post_hook_failure_saves_real_result_and_pairs_unexecuted_calls(runtime_factory, monkeypatch):
    runtime, _ = runtime_factory([response(tools=[
        tool("write_file", "first", path="first.txt", content="written"),
        tool("write_file", "second", path="second.txt", content="never")], stop="tool_use")])

    def fail(*args):
        raise RuntimeError("post failed")

    monkeypatch.setitem(hooks.HOOKS, "PostToolUse", [fail])
    submitted = runtime.submit("write")
    finished, _ = until(runtime, "turn_finished", submission_id=submitted.submission_id)
    snapshot = stored(runtime)
    assert finished.payload["outcome"]["status"] == "failed"
    results = snapshot["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "first" and not results[0].get("is_error")
    assert results[1]["tool_use_id"] == "second" and results[1]["is_error"]
    assert (runtime.session.store.workdir / "first.txt").read_text() == "written"
    assert not (runtime.session.store.workdir / "second.txt").exists()
    assert any(item["payload"].get("phase") == "hook" and item["payload"].get("state") == "failed"
               for item in records(snapshot, "execution_status"))
    restored = runtime.session.store.load(snapshot["id"])
    assert restored["messages"] == snapshot["messages"]


def test_tool_status_comes_from_execution_not_output(host_factory):
    host_factory()
    output = file_tools.run_bash("printf 'Error: sample output'")
    assert output.content == "Error: sample output"
    assert output.status == "success"
    failed = file_tools.run_bash("printf done; exit 2")
    assert failed.status == "failed" and "done" in failed.content
    task = tasks.create_task("pending")
    result = shared_tools.run_complete_task(task.id)
    assert result.status == "failed" and "cannot complete" in result.content


def test_failed_tool_does_not_skip_other_calls_in_batch(host_factory, monkeypatch):
    from agent_harness import core
    import copy
    from types import SimpleNamespace
    host = host_factory()
    calls = []
    responses = iter([response(tools=[
        tool("read_file", "missing", path="missing.txt"),
        tool("write_file", "write", path="result.txt", content="done")], stop="tool_use"), response()])

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs["messages"]))
        return next(responses)

    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    host.agent_loop([{"role": "user", "content": "work"}], "work")
    results = calls[1][-1]["content"]
    assert results[0]["tool_use_id"] == "missing" and results[0]["is_error"]
    assert results[1]["tool_use_id"] == "write" and not results[1].get("is_error")
    assert (core.WORKDIR / "result.txt").read_text() == "done"


def test_compact_failure_is_saved_as_a_new_correction_fact(runtime_factory, monkeypatch):
    from agent_harness.context import compaction
    runtime, _ = runtime_factory([
        response(tools=[tool("compact", "compact")], stop="tool_use"), response()])

    def fail_summary(messages):
        raise RuntimeError("summary unavailable")

    monkeypatch.setattr(compaction, "summarize_history", fail_summary)
    submitted = runtime.submit("compact")
    until(runtime, "turn_finished", submission_id=submitted.submission_id)
    snapshot = stored(runtime)
    results = [item for item in records(snapshot, "tool_result")
               if item["payload"]["tool_use_id"] == "compact"]
    assert [item["payload"]["status"] for item in results] == ["returned", "failed"]
    assert results[0]["id"] != results[1]["id"]
    assert "Compaction requested" in results[0]["payload"]["content"]
    assert results[1]["payload"]["correction"] and results[1]["payload"]["is_error"]
    assert snapshot["messages"][-2]["content"][0]["is_error"]
