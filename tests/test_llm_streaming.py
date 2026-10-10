"""Exercise actual HTTP streaming without a remote model or API key."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from agent_harness.providers import llm_client


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


def client_for(protocol, url):
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
                results.append(llm_client.stream_message(client_for(protocol, url), on_text=on_text, stopped=lambda: False,
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
def test_eof_after_partial_text_is_not_a_completed_message(protocol):
    with server_for(frames(protocol, final=False)) as (url, _):
        parts = []
        with pytest.raises(llm_client.ProtocolResponseError):
            llm_client.stream_message(client_for(protocol, url), on_text=lambda i, t: parts.append(t),
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
            llm_client.stream_message(client_for("responses", url), on_text=lambda *args: None,
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
            llm_client.stream_message(client_for("anthropic", url), on_text=lambda *args: pytest.fail("tool arguments shown as text"),
                                      stopped=lambda: False, model="test", messages=[], max_tokens=100)
