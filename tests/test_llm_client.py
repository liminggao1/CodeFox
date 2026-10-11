"""模型协议校验：messages.create 消费接口，以及不依赖远端模型的真实 HTTP 流式响应。"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import asyncio
import json
import threading

import pytest

from agent_harness.agents import recovery
from agent_harness.providers import llm_client
import agent_harness.agents.goal as goal_module
import agent_harness.agents.goal_evaluator as evaluator_module
import agent_harness.core as core
import agent_harness.runtime.hooks as hooks
import agent_harness.runtime.scheduler as scheduler
import agent_harness.workflows.workflow_runtime as workflow_runtime


def responses_tool(arguments="{}", name="read_file", call_id="call-1"):
    """Responses 协议输出里的工具调用条目，参数保持原始 JSON 字符串。"""
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}


def stubbed_client(monkeypatch, response):
    """构造 ResponsesClient，并把网络层替换成给定响应。"""
    client = llm_client.ResponsesClient(api_key="fake")
    monkeypatch.setattr(client, "_post", lambda payload: response)
    return client


def create(client):
    return client.messages.create(model="test", messages=[{"role": "user", "content": "go"}])


def test_failed_tool_result_uses_supported_responses_output(monkeypatch):
    from agent_harness.tools.tool_result import ToolResult
    payloads = []
    client = llm_client.ResponsesClient(api_key="fake")

    def post(payload):
        payloads.append(payload)
        return {"status": "completed", "output": []}

    monkeypatch.setattr(client, "_post", post)
    client.messages.create(model="test", messages=[{"role": "user", "content": [
        ToolResult("Blocked by dependencies", "failed").model_result("blocked"),
        ToolResult("Error: successful output", "success").model_result("success")]}])
    outputs = payloads[0]["input"]
    assert outputs[0] == {"type": "function_call_output", "call_id": "blocked",
                          "output": "[Tool execution failed]\nBlocked by dependencies"}
    assert outputs[1]["output"] == "Error: successful output"


def test_completed_text_tool_and_usage(monkeypatch):
    response = {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
        responses_tool('{"path":"你好.txt"}'), responses_tool({}, name="list_tasks", call_id="call-2"),
    ], "usage": {"input_tokens": 12, "output_tokens": 4}}
    message = create(stubbed_client(monkeypatch, response))
    assert message.stop_reason == "tool_use"
    assert message.content[0].text == "hello"
    assert message.content[1].input == {"path": "你好.txt"}
    assert message.content[2].input == {}
    assert (message.usage.input_tokens, message.usage.output_tokens) == (12, 4)


def test_plain_json_text_is_not_executed_as_a_tool(host_factory, monkeypatch):
    host = host_factory()
    text = '{"name":"write_file","input":{"path":"unwanted.txt","content":"must not run"}}'
    client = stubbed_client(monkeypatch, {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]},
    ]})
    monkeypatch.setattr(core, "client", client)
    messages = [{"role": "user", "content": "展示工具调用格式"}]
    outcome = host.agent_loop(messages, "展示工具调用格式")
    assert outcome.status == "completed"
    assert messages[-1]["content"][0]["text"] == text
    assert not (core.WORKDIR / "unwanted.txt").exists()


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress", None, "unknown", []])
def test_invalid_status_is_not_normal_completion(monkeypatch, status):
    with pytest.raises(llm_client.ProtocolResponseError):
        create(stubbed_client(monkeypatch, {"status": status, "output": []}))


@pytest.mark.parametrize("arguments", [None, "", " ", '{"path":', "[]", "true", "1", '"path"', []])
def test_invalid_arguments_fail_the_entire_tool_batch(monkeypatch, arguments):
    client = stubbed_client(monkeypatch, {"status": "completed", "output": [responses_tool(), responses_tool(arguments)]})
    with pytest.raises(llm_client.ProtocolResponseError) as error:
        create(client)
    assert error.value.tool_name == "read_file"


@pytest.mark.parametrize("item", [responses_tool(name=None), responses_tool(name=" "), responses_tool(call_id=None), responses_tool(call_id=" ")])
def test_missing_tool_identity(monkeypatch, item):
    with pytest.raises(llm_client.ProtocolResponseError):
        create(stubbed_client(monkeypatch, {"status": "completed", "output": [item]}))


def test_valid_id_is_used_when_call_id_is_unusable(monkeypatch):
    for call_id in (None, "", " ", 1):
        item = dict(responses_tool(call_id=call_id), id="fallback-id")
        message = create(stubbed_client(monkeypatch, {"status": "completed", "output": [item]}))
        assert message.content[0].id == "fallback-id"


def test_incomplete_text_can_continue_but_incomplete_tools_cannot(monkeypatch):
    response = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}]}
    message = create(stubbed_client(monkeypatch, response))
    assert message.stop_reason == "max_tokens"
    assert message.content[0].text == "partial"
    response["output"].append(responses_tool())
    with pytest.raises(llm_client.ProtocolResponseError):
        create(stubbed_client(monkeypatch, response))


@pytest.mark.parametrize("response", [None, [], {}, {"status": "incomplete"},
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}])
def test_missing_status_or_unknown_incomplete_reason(monkeypatch, response):
    with pytest.raises(llm_client.ProtocolResponseError):
        create(stubbed_client(monkeypatch, response))


def test_completed_empty_and_zero_argument_calls_are_valid(monkeypatch):
    assert create(stubbed_client(monkeypatch, {"status": "completed", "output": []})).content == []
    assert create(stubbed_client(monkeypatch, {"status": "completed", "output": [responses_tool()]})).content[0].input == {}


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
    client = stubbed_client(monkeypatch, {"status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [responses_tool('{"path":"bad.txt","content":"must not run"}', "write_file")]})
    monkeypatch.setattr(core, "client", client)
    stops = []
    monkeypatch.setattr(hooks, "trigger_hooks", lambda event, *args: stops.append(event))
    job = scheduler.schedule_job("* * * * *", "scheduled work", recurring=False)
    scheduler._enqueue_due_job(job)
    messages = [{"role": "user", "content": "go"}]
    host.agent_loop(messages, "go")
    assert "Stop" not in stops
    assert scheduler._cron_queue == [job]
    assert not (core.WORKDIR / "bad.txt").exists()
    assert not any(message.get("role") == "assistant" and "ProtocolResponseError" in str(message.get("content"))
                   for message in messages)
    assert not any("tool_use" in str(msg) for msg in messages)


def test_workflow_protocol_failure_writes_no_successful_agent_journal(host_factory, monkeypatch):
    host = host_factory()
    client = stubbed_client(monkeypatch, {"status": "failed", "output": []})
    runtime = workflow_runtime
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
    client = stubbed_client(monkeypatch, {"status": "failed", "output": []})
    evaluator = evaluator_module.PromptGoalEvaluator(client=client, model="test")
    goal = goal_module.GoalController(evaluator=evaluator)
    goal.set_goal("tests pass")
    result = asyncio.run(goal.evaluate_after_turn([]))
    assert result.action == "error"
    assert "ProtocolResponseError" in result.reason
    assert goal.active.condition == "tests pass"


def frame(data):
    return ("event: " + data["type"] + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


def frames(protocol, text="你好", final=True):
    if protocol == "responses":
        events = [{"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text}]
        if final:
            events.append({"type": "response.completed", "response": {
                "id": "r", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 7, "output_tokens": 2}}})
    else:
        events = [{"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "test",
                   "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 0}}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}]
        if final:
            events.extend([{"type": "content_block_stop", "index": 0},
                           {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}},
                           {"type": "message_stop"}])
    return [frame(event) for event in events]


@contextmanager
def server_for(chunks, *, pause_after=None, release=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for index, chunk in enumerate(chunks):
                    # Splitting Chinese UTF-8 and frame delimiters is intentional.
                    for offset in range(0, len(chunk), 2):
                        self.wfile.write(chunk[offset:offset + 2])
                        self.wfile.flush()
                    if index == pause_after:
                        assert release.wait(5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        if release is not None:
            release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def live_client(protocol, url):
    """按协议构造指向本地测试服务器的真实客户端。"""
    if protocol == "responses":
        return llm_client.ResponsesClient(base_url=url, api_key="offline", timeout=3)
    import anthropic
    return anthropic.Anthropic(base_url=url, api_key="offline", timeout=3, max_retries=0)


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_first_text_arrives_before_complete_response(protocol):
    visible, release = threading.Event(), threading.Event()
    parts, results, failures = [], [], []
    def on_text(index, text):
        parts.append((index, text))
        visible.set()
    with server_for(frames(protocol), pause_after=0 if protocol == "responses" else 2, release=release) as (url, received):
        def run():
            try:
                results.append(llm_client.stream_message(live_client(protocol, url), on_text=on_text, stopped=lambda: False,
                               model="test", messages=[{"role": "user", "content": "go"}], max_tokens=100))
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert visible.wait(3), failures
            assert parts == [("0:0" if protocol == "responses" else 0, "你好")]
            assert not results
        finally:
            release.set()
            thread.join(4)
        assert not failures and not thread.is_alive()
        assert results[0].content[0].text == "你好"
        assert results[0].usage.output_tokens == 2
        assert received[0]["stream"] is True


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_streamed_native_tools_have_parsed_arguments_and_matching_results(protocol):
    arguments = {"todos": [{"content": "读取中文文件", "status": "in_progress"}]}
    raw = json.dumps(arguments, ensure_ascii=False)
    if protocol == "responses":
        chunks = [frame({"type": "response.completed", "response": {
            "id": "r", "status": "completed", "output": [
                {"type": "function_call", "name": "todo_write", "call_id": "todo", "arguments": raw},
                {"type": "function_call", "name": "list_tasks", "call_id": "tasks", "arguments": "{}"},
            ], "usage": {"input_tokens": 7, "output_tokens": 2}}})]
    else:
        chunks = frames(protocol, final=False)[:1]
        for index, (name, call_id, value) in enumerate([
                ("todo_write", "todo", raw), ("list_tasks", "tasks", "{}")]):
            chunks.append(frame({"type": "content_block_start", "index": index,
                                 "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}}))
            # 将工具参数分成两段，验证消费器等待完整 JSON 后才接受调用。
            for partial in (value[:len(value) // 2], value[len(value) // 2:]):
                chunks.append(frame({"type": "content_block_delta", "index": index,
                                     "delta": {"type": "input_json_delta", "partial_json": partial}}))
            chunks.append(frame({"type": "content_block_stop", "index": index}))
        chunks.extend([
            frame({"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                   "usage": {"output_tokens": 2}}),
            frame({"type": "message_stop"}),
        ])
    with server_for(chunks) as (url, received):
        client = live_client(protocol, url)
        message = llm_client.stream_message(
            client, on_text=lambda *args: pytest.fail("工具参数不能作为正文展示"),
            stopped=lambda: False, model="test", messages=[], max_tokens=100)
        assert message.stop_reason == "tool_use"
        assert [(block.name, block.id, block.input) for block in message.content] == [
            ("todo_write", "todo", arguments), ("list_tasks", "tasks", {})]
        results = [{"type": "tool_result", "tool_use_id": "todo", "content": "updated"},
                   {"type": "tool_result", "tool_use_id": "tasks", "content": "listed"}]
        llm_client.stream_message(client, on_text=lambda *args: None, stopped=lambda: False,
                                  model="test", max_tokens=100, messages=[
                                      {"role": "assistant", "content": message.content},
                                      {"role": "user", "content": results}])
        if protocol == "responses":
            outputs = [item for item in received[1]["input"] if item["type"] == "function_call_output"]
            assert [(item["call_id"], item["output"]) for item in outputs] == [("todo", "updated"), ("tasks", "listed")]
        else:
            assert received[1]["messages"][-1]["content"] == results


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_eof_after_partial_text_is_not_a_completed_message(protocol):
    with server_for(frames(protocol, final=False)) as (url, _):
        parts = []
        with pytest.raises(llm_client.ProtocolResponseError):
            llm_client.stream_message(live_client(protocol, url), on_text=lambda i, t: parts.append(t),
                                      stopped=lambda: False, model="test", messages=[], max_tokens=100)
        assert parts == ["你好"]
@pytest.mark.parametrize("final", [
    {"type": "response.failed", "response": {"status": "failed", "error": {"message": "rejected"}}},
    {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "function_call", "name": "write_file", "call_id": "tool", "arguments": '{"path":'}]}},
    {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"type": "function_call", "name": "write_file", "call_id": "tool", "arguments": "{}"}]}},
    {"type": "error", "message": "model failure"},
])
def test_responses_failed_or_partial_tool_response_is_rejected(final):
    with server_for(frames("responses", final=False) + [frame(final)]) as (url, _):
        with pytest.raises(llm_client.ProtocolResponseError):
            llm_client.stream_message(live_client("responses", url), on_text=lambda *args: None,
                                      stopped=lambda: False, model="test", messages=[], max_tokens=100)


@pytest.mark.parametrize("ending", ["end_turn", "max_tokens"])
def test_anthropic_partial_tool_json_is_rejected(ending):
    chunks = frames("anthropic", final=False)[:1]
    chunks += [frame({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool", "name": "write_file", "input": {}}}),
               frame({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"'}}),
               frame({"type": "content_block_stop", "index": 0}),
               frame({"type": "message_delta", "delta": {"stop_reason": ending, "stop_sequence": None}, "usage": {"output_tokens": 2}}),
               frame({"type": "message_stop"})]
    with server_for(chunks) as (url, _):
        with pytest.raises(llm_client.ProtocolResponseError):
            llm_client.stream_message(live_client("anthropic", url), on_text=lambda *args: pytest.fail("tool arguments shown as text"),
                                      stopped=lambda: False, model="test", messages=[], max_tokens=100)
