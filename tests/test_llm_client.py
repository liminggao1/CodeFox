"""Protocol validation through the messages.create consumer interface."""

from types import SimpleNamespace
import asyncio

import pytest

from agent_harness.providers import llm_client
from agent_harness.agents import recovery


def tool(arguments="{}", name="read_file", call_id="call-1"):
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}


def client_for(monkeypatch, response):
    client = llm_client.ResponsesClient(api_key="fake")
    monkeypatch.setattr(client, "_post", lambda payload: response)
    return client


def create(client):
    return client.messages.create(model="test", messages=[{"role": "user", "content": "go"}])


def test_completed_text_tool_and_usage(monkeypatch):
    response = {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
        tool('{"path":"你好.txt"}'), tool({}, name="list_tasks", call_id="call-2"),
    ], "usage": {"input_tokens": 12, "output_tokens": 4}}
    message = create(client_for(monkeypatch, response))
    assert message.stop_reason == "tool_use"
    assert message.content[0].text == "hello"
    assert message.content[1].input == {"path": "你好.txt"}
    assert message.content[2].input == {}
    assert (message.usage.input_tokens, message.usage.output_tokens) == (12, 4)


def test_plain_json_text_is_not_executed_as_a_tool(host_factory, monkeypatch):
    host = host_factory()
    text = '{"name":"write_file","input":{"path":"unwanted.txt","content":"must not run"}}'
    client = client_for(monkeypatch, {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]},
    ]})
    monkeypatch.setattr(host.core, "client", client)
    messages = [{"role": "user", "content": "展示工具调用格式"}]
    outcome = host.agent_loop(messages, "展示工具调用格式")
    assert outcome.status == "completed"
    assert messages[-1]["content"][0]["text"] == text
    assert not (host.core.WORKDIR / "unwanted.txt").exists()


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress", None, "unknown", []])
def test_invalid_status_is_not_normal_completion(monkeypatch, status):
    with pytest.raises(llm_client.ProtocolResponseError):
        create(client_for(monkeypatch, {"status": status, "output": []}))


@pytest.mark.parametrize("arguments", [None, "", " ", '{"path":', "[]", "true", "1", '"path"', []])
def test_invalid_arguments_fail_the_entire_tool_batch(monkeypatch, arguments):
    client = client_for(monkeypatch, {"status": "completed", "output": [tool(), tool(arguments)]})
    with pytest.raises(llm_client.ProtocolResponseError) as error:
        create(client)
    assert error.value.tool_name == "read_file"


@pytest.mark.parametrize("item", [tool(name=None), tool(name=" "), tool(call_id=None), tool(call_id=" ")])
def test_missing_tool_identity(monkeypatch, item):
    with pytest.raises(llm_client.ProtocolResponseError):
        create(client_for(monkeypatch, {"status": "completed", "output": [item]}))


def test_valid_id_is_used_when_call_id_is_unusable(monkeypatch):
    for call_id in (None, "", " ", 1):
        item = dict(tool(call_id=call_id), id="fallback-id")
        message = create(client_for(monkeypatch, {"status": "completed", "output": [item]}))
        assert message.content[0].id == "fallback-id"


def test_incomplete_text_can_continue_but_incomplete_tools_cannot(monkeypatch):
    response = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}]}
    message = create(client_for(monkeypatch, response))
    assert message.stop_reason == "max_tokens"
    assert message.content[0].text == "partial"
    response["output"].append(tool())
    with pytest.raises(llm_client.ProtocolResponseError):
        create(client_for(monkeypatch, response))


@pytest.mark.parametrize("response", [None, [], {}, {"status": "incomplete"},
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}])
def test_missing_status_or_unknown_incomplete_reason(monkeypatch, response):
    with pytest.raises(llm_client.ProtocolResponseError):
        create(client_for(monkeypatch, response))


def test_completed_empty_and_zero_argument_calls_are_valid(monkeypatch):
    assert create(client_for(monkeypatch, {"status": "completed", "output": []})).content == []
    assert create(client_for(monkeypatch, {"status": "completed", "output": [tool()]})).content[0].input == {}


def test_protocol_error_is_not_retried_or_recovered(monkeypatch):
    calls = []
    error = llm_client.ProtocolResponseError("failed", "529 overloaded 429 context_length_exceeded")

    def fail():
        calls.append(True)
        raise error

    monkeypatch.setattr(recovery.time, "sleep", lambda _: pytest.fail("protocol error was retried"))
    with pytest.raises(llm_client.ProtocolResponseError):
        recovery.with_retry(fail, recovery.RecoveryState())
    assert calls == [True]
    assert not recovery.is_prompt_too_long_error(error)


def test_lead_protocol_failure_executes_no_tools_or_stop_and_restores_cron(host_factory, monkeypatch):
    host = host_factory()
    client = client_for(monkeypatch, {"status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [tool('{"path":"bad.txt","content":"must not run"}', "write_file")]})
    monkeypatch.setattr(host.core, "client", client)
    stops = []
    monkeypatch.setattr(host.hooks, "trigger_hooks", lambda event, *args: stops.append(event))
    job = host.scheduler.schedule_job("* * * * *", "scheduled work", recurring=False)
    host.scheduler._enqueue_due_job(job)
    messages = [{"role": "user", "content": "go"}]
    host.agent_loop(messages, "go")
    assert "Stop" not in stops
    assert host.scheduler.cron_queue == [job]
    assert not (host.core.WORKDIR / "bad.txt").exists()
    assert "ProtocolResponseError" in messages[-1]["content"][0]["text"]
    assert not any("tool_use" in str(msg) for msg in messages)


def test_workflow_protocol_failure_writes_no_successful_agent_journal(host_factory, monkeypatch):
    host = host_factory()
    client = client_for(monkeypatch, {"status": "failed", "output": []})
    runtime = host.workflow_runtime
    monkeypatch.setattr(runtime, "RUNNER_FACTORY", lambda: runtime.SubagentRunner(client, "test"))

    async def step(ctx, args):
        return await ctx.agent("one step", label="first")

    out = asyncio.run(runtime.WorkflowTool().call(
        {"name": "protocol-failure", "description": "test"}, step))
    assert out["task"].status == "failed"
    assert "Invalid model response" in out["result"]["error"]
    journal = runtime.store_dir() / f"{out['task'].run_id}.journal.jsonl"
    assert journal.read_text(encoding="utf-8") == ""


def test_goal_protocol_failure_preserves_active_condition(host_factory, monkeypatch):
    host = host_factory()
    client = client_for(monkeypatch, {"status": "failed", "output": []})
    evaluator = host.goal_module.PromptGoalEvaluator(client=client, model="test")
    goal = host.goal_module.GoalController(evaluator=evaluator)
    goal.set_goal("tests pass")
    result = asyncio.run(goal.evaluate_after_turn([]))
    assert result.action == "error"
    assert "ProtocolResponseError" in result.reason
    assert goal.active.condition == "tests pass"
