"""与模型提供方无关的 LLM 客户端适配层。

调用方这样创建客户端::

    from llm_client import Anthropic

    client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    response = client.messages.create(model=MODEL, system=SYSTEM,
                                      messages=messages, tools=TOOLS,
                                      max_tokens=8000)

本模块保持该接口形状，同时支持不同的传输协议。

默认返回真正的 ``anthropic.Anthropic`` 客户端，兼容 Anthropic 的端点不受影响。

设置 ``LLM_API=responses`` 时，返回使用 OpenAI Responses API
（``POST /v1/responses``）的适配器，自动转换请求与响应；调用方无需感知差异。
"""

import json
import math
import os
import re
import urllib.error
import urllib.request

__all__ = ["Anthropic", "ProtocolResponseError", "StreamStopped", "stream_message"]

DEFAULT_TIMEOUT = 300.0
ERROR_BODY_PREVIEW_CHARS = 500
ERROR_DETAIL_PREVIEW_CHARS = 1000
USER_AGENT = "learncc-llm-client/1.0"


_CJK_TOKEN = re.compile(r"[\u3400-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")


def estimate_input_tokens(system=None, messages=None, tools=None) -> int:
    """Estimate request tokens when a compatible count endpoint is unavailable."""
    payload = json.dumps({"system": system or "", "messages": messages or [],
                          "tools": tools or []}, ensure_ascii=False, default=str)
    tokens = 0
    index = 0
    while index < len(payload):
        char = payload[index]
        if _CJK_TOKEN.match(char):
            tokens += 1
            index += 1
            continue
        word = _ASCII_WORD.match(payload, index)
        if word:
            tokens += max(1, math.ceil(len(word.group()) / 4))
            index = word.end()
            continue
        if not char.isspace():
            tokens += 1
        index += 1
    return tokens


def count_input_tokens(client, *, model, system=None, messages=None, tools=None) -> int:
    """Estimate request tokens without adding a second network request."""
    request = dict(model=model, messages=messages or [])
    if system:
        request["system"] = system
    if tools:
        request["tools"] = tools
    counter = getattr(client, "count_input_tokens", None)
    if callable(counter):
        result = counter(**request)
        value = result.get("input_tokens") if isinstance(result, dict) else result
        if type(value) is int and value >= 0:
            return value
    return estimate_input_tokens(system, messages, tools)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _use_responses() -> bool:
    return os.getenv("LLM_API", "").strip().lower() in {
        "responses", "openai", "openai-responses",
    }


def _default_endpoint() -> str:
    explicit = os.getenv("LLM_RESPONSES_URL")
    if explicit:
        return explicit.rstrip("/")
    return _responses_url(
        os.getenv("OPENAI_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL") or ""
    )


def _responses_url(base_url: str) -> str:
    base = (base_url or "https://api.openai.com").rstrip("/")
    if base.endswith("/v1"):
        return base + "/responses"
    return base + "/v1/responses"


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""


# ---------------------------------------------------------------------------
# 错误名称与 SDK 保持一致，以便复用现有重试判断
# ---------------------------------------------------------------------------

class LLMClientError(Exception):
    """本适配层抛出的错误基类。"""


class APIConnectionError(LLMClientError):
    """无法连接端点。"""


class ProtocolResponseError(LLMClientError):
    """The response cannot be consumed as a completed model result."""

    def __init__(self, status, reason, tool_name=None, tool_call_id=None):
        super().__init__(f"Invalid model response ({status}): {reason}")
        self.status = status
        self.reason = reason
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id


class StreamStopped(LLMClientError):
    """The caller cancelled before accepting a complete response."""


class APIStatusError(LLMClientError):
    """端点返回非 2xx 响应。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BadRequestError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class NotFoundError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


_ERROR_BY_STATUS = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    429: RateLimitError,
}


def _error_for(status: int, detail: str) -> APIStatusError:
    error_class = _ERROR_BY_STATUS.get(status)
    if error_class is None:
        error_class = InternalServerError if status >= 500 else APIStatusError
    return error_class(f"{status} {detail}", status_code=status)


# ---------------------------------------------------------------------------
# 与 Messages 接口一致的数据容器
# ---------------------------------------------------------------------------

class Block(dict):
    """同时支持属性访问的内容块。

    调用方读取 ``block.type``、``block.text``、``block.input`` 和
    ``block.name``；转录与测试则按字典读取。字典加属性回退兼容两种用法。
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class Message:
    """返回给调用方的 Messages 形式响应。"""

    def __init__(self, content, stop_reason, usage=None, model=None, id=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage
        self.model = model
        self.id = id
        self.role = "assistant"
        self.type = "message"


# ---------------------------------------------------------------------------
# 请求转换：Messages -> Responses
# ---------------------------------------------------------------------------

def _field(block, name, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text_of(_field(part, "text", "")) for part in value)
    return str(value)


def _tool_definition(tool) -> dict:
    return {
        "type": "function",
        "name": _field(tool, "name"),
        "description": _field(tool, "description", "") or "",
        "parameters": _field(tool, "input_schema")
        or {"type": "object", "properties": {}},
    }


def _message_items(role: str, content) -> list:
    """将一条 Messages 消息转换为 Responses 输入项。"""
    text_kind = "input_text" if role == "user" else "output_text"
    if isinstance(content, str):
        return [{
            "type": "message", "role": role,
            "content": [{"type": text_kind, "text": content}],
        }]
    items = []
    for block in content or []:
        kind = _field(block, "type")
        if kind == "text":
            items.append({
                "type": "message", "role": role,
                "content": [{"type": text_kind, "text": _field(block, "text", "")}],
            })
        elif kind == "tool_use":
            items.append({
                "type": "function_call",
                "call_id": _field(block, "id"),
                "name": _field(block, "name"),
                "arguments": json.dumps(_field(block, "input", {}) or {},
                                        ensure_ascii=False),
            })
        elif kind == "tool_result":
            output = _text_of(_field(block, "content", ""))
            if _field(block, "is_error", False):
                output = "[Tool execution failed]\n" + output
            items.append({
                "type": "function_call_output",
                "call_id": _field(block, "tool_use_id"),
                "output": output,
            })
    return items


def _to_responses_request(model, messages, system, tools, max_tokens) -> dict:
    payload = {"model": model, "input": []}
    if system:
        payload["instructions"] = _text_of(system)
    if max_tokens:
        payload["max_output_tokens"] = max_tokens
    if tools:
        payload["tools"] = [_tool_definition(tool) for tool in tools]
    for message in messages or []:
        role = _field(message, "role") or "user"
        payload["input"].extend(_message_items(role, _field(message, "content")))
    return payload


# ---------------------------------------------------------------------------
# 响应转换：Responses -> Messages
# ---------------------------------------------------------------------------

def _parse_arguments(raw, *, status, tool_name, tool_call_id):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolResponseError(status, "invalid function arguments", tool_name, tool_call_id) from exc
    if not isinstance(parsed, dict):
        raise ProtocolResponseError(status, "function arguments must be an object", tool_name, tool_call_id)
    return parsed


def _stop_reason(response: dict, has_tool_call: bool) -> str:
    if has_tool_call:
        return "tool_use"
    if response.get("status") == "incomplete":
        return "max_tokens"
    return "end_turn"


def _to_message(response: dict, fallback_model) -> Message:
    if not isinstance(response, dict):
        raise ProtocolResponseError(None, "response must be an object")
    status = response.get("status")
    details = response.get("incomplete_details") or {}
    reason = details.get("reason") if isinstance(details, dict) else None
    if not isinstance(status, str) or status not in {"completed", "incomplete"}:
        error = response.get("error") or {}
        reason = error.get("message") if isinstance(error, dict) else str(error)
        raise ProtocolResponseError(status, reason or "response did not complete")
    output = response.get("output") or []
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise ProtocolResponseError(status, "output must be a list of objects")
    if status == "incomplete":
        if reason != "max_output_tokens":
            raise ProtocolResponseError(status, reason or "missing incomplete reason")
        if any(item.get("type") == "function_call" for item in output):
            raise ProtocolResponseError(status, "incomplete function call batch")
    blocks = []
    has_tool_call = False
    text_indices = {}
    for output_index, item in enumerate(output):
        kind = item.get("type")
        if kind == "message":
            for content_index, part in enumerate(item.get("content") or []):
                if part.get("type") in ("output_text", "text"):
                    text_indices[len(blocks)] = f"{output_index}:{content_index}"
                    blocks.append(Block(type="text", text=part.get("text", "")))
        elif kind == "function_call":
            name = item.get("name")
            call_id = next((value for value in (item.get("call_id"), item.get("id"))
                            if isinstance(value, str) and value.strip()), None)
            if not isinstance(name, str) or not name.strip():
                raise ProtocolResponseError(status, "missing function name", name, call_id)
            if not isinstance(call_id, str) or not call_id.strip():
                raise ProtocolResponseError(status, "missing function call identity", name, call_id)
            has_tool_call = True
            blocks.append(Block(
                type="tool_use",
                id=call_id,
                name=name,
                input=_parse_arguments(item.get("arguments"), status=status,
                                       tool_name=name, tool_call_id=call_id),
            ))
    usage = response.get("usage") or {}
    message = Message(
        content=blocks,
        stop_reason=_stop_reason(response, has_tool_call),
        usage=Block(input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0)),
        model=response.get("model") or fallback_model,
        id=response.get("id"),
    )
    message.stream_indices = text_indices
    return message


def _sse_events(response, stopped):
    """Read complete UTF-8 SSE lines across arbitrary network boundaries."""
    buffer, data = b"", []
    while True:
        if stopped():
            raise StreamStopped("Stopped while waiting for model output")
        chunk = response.read1(4096)
        if stopped():
            raise StreamStopped("Stopped while receiving model output")
        buffer += chunk
        while match := re.search(br"\r\n|\r|\n", buffer):
            if match.end() == len(buffer) and buffer.endswith(b"\r") and chunk:
                break
            raw, buffer = buffer[:match.start()], buffer[match.end():]
            try:
                line = raw.decode("utf-8")
            except UnicodeError as exc:
                raise ProtocolResponseError("stream", "invalid UTF-8 event") from exc
            if not line:
                if data:
                    try:
                        value = json.loads("\n".join(data))
                    except ValueError as exc:
                        raise ProtocolResponseError("stream", "invalid event JSON") from exc
                    if not isinstance(value, dict):
                        raise ProtocolResponseError("stream", "event must be an object")
                    yield value
                    data = []
            elif line.startswith("data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(" ") else value)
        if not chunk:
            return


# ---------------------------------------------------------------------------
# 网络客户端
# ---------------------------------------------------------------------------

def _describe(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8", "replace")
    except Exception:
        return str(error.reason or "request failed")
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:ERROR_BODY_PREVIEW_CHARS] or str(error.reason or "request failed")
    detail = parsed.get("error", parsed)
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
    return str(detail)[:ERROR_DETAIL_PREVIEW_CHARS]


class _Messages:
    """向调用方提供 ``create`` 方法。"""

    def __init__(self, client):
        self._client = client

    def create(self, **kwargs) -> Message:
        return self._client.create(**kwargs)


class ResponsesClient:
    """基于 OpenAI Responses API 的 ``messages.create`` 客户端。"""

    def __init__(self, base_url=None, api_key=None, timeout=None, **_ignored):
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or os.getenv("ANTHROPIC_BASE_URL"))
        self.api_key = api_key or _api_key()
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.messages = _Messages(self)

    def create(self, model, messages, system=None, tools=None,
               max_tokens=None, **_ignored) -> Message:
        payload = _to_responses_request(model, messages, system, tools, max_tokens)
        return _to_message(self._post(payload), model)

    def stream(self, *, on_text, stopped, model, messages, system=None, tools=None,
               max_tokens=None, **_ignored):
        payload = _to_responses_request(model, messages, system, tools, max_tokens)
        payload["stream"] = True
        headers = dict(self._headers(), Accept="text/event-stream")
        request = urllib.request.Request(self._endpoint(), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for event in _sse_events(response, stopped):
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        output, content, text = event.get("output_index"), event.get("content_index"), event.get("delta")
                        if (type(output) is not int or output < 0 or type(content) is not int or content < 0
                                or not isinstance(text, str)):
                            raise ProtocolResponseError("stream", "invalid text delta")
                        if text:
                            on_text(f"{output}:{content}", text)
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        final = event.get("response")
                        if not isinstance(final, dict) or final.get("status") != kind.split(".")[1]:
                            raise ProtocolResponseError("stream", "terminal event/status mismatch")
                        return _to_message(final, model)
                    elif kind == "error":
                        code, message = event.get("code"), event.get("message", "stream error")
                        if code == "rate_limit_exceeded":
                            raise _error_for(429, message)
                        if code == "overloaded":
                            raise _error_for(529, message)
                        raise ProtocolResponseError("failed", str(message))
        except urllib.error.HTTPError as error:
            raise _error_for(error.code, _describe(error)) from None
        except urllib.error.URLError as error:
            raise APIConnectionError(f"Cannot reach {self._endpoint()}: {error.reason}") from None
        raise ProtocolResponseError("stream", "stream ended without a terminal response")

    def _endpoint(self) -> str:
        if self.base_url:
            return _responses_url(self.base_url)
        return _default_endpoint()

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raise _error_for(error.code, _describe(error)) from None
        except urllib.error.URLError as error:
            raise APIConnectionError(
                f"Cannot reach {self._endpoint()}: {error.reason}") from None
        try:
            return json.loads(body)
        except ValueError:
            raise APIStatusError(
                f"Invalid JSON from {self._endpoint()}: {body[:ERROR_BODY_PREVIEW_CHARS]}") from None

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def stream_message(client, *, on_text, stopped, **request):
    """Stream visible text synchronously and return only a validated full message."""
    if stopped():
        raise StreamStopped("Stopped before requesting model")
    if isinstance(client, ResponsesClient):
        return client.stream(on_text=on_text, stopped=stopped, **request)
    if hasattr(client, "with_options"):
        # Recovery owns each observable attempt; disable hidden SDK retries.
        client = client.with_options(max_retries=0)
    # The SDK assembles the message; raw deltas also let us require a real end
    # and validate complete tool JSON rather than its partial JSON snapshot.
    finished, inputs, opened = False, {}, set()
    with client.messages.stream(**request) as stream:
        for event in stream:
            if stopped():
                raise StreamStopped("Stopped while receiving model output")
            if event.type == "content_block_start":
                opened.add(event.index)
                if event.content_block.type == "tool_use":
                    inputs[event.index] = ""
            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    on_text(event.index, event.delta.text)
                elif event.delta.type == "input_json_delta":
                    inputs[event.index] = inputs.get(event.index, "") + event.delta.partial_json
            elif event.type == "content_block_stop":
                opened.discard(event.index)
            elif event.type == "message_stop":
                finished = True
        if stopped():
            raise StreamStopped("Stopped before accepting model output")
        if not finished or opened:
            raise ProtocolResponseError("stream", "stream ended without a complete message")
        message = stream.get_final_message()
        if message.stop_reason not in {"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"}:
            raise ProtocolResponseError(message.stop_reason, "missing or invalid stop reason")
        for index, raw in inputs.items():
            block = message.content[index]
            if message.stop_reason == "max_tokens":
                raise ProtocolResponseError("max_tokens", "incomplete tool batch", block.name, block.id)
            if not isinstance(block.id, str) or not block.id.strip() or not isinstance(block.name, str) or not block.name.strip():
                raise ProtocolResponseError(message.stop_reason, "invalid tool identity")
            _parse_arguments(raw or "{}", status=message.stop_reason, tool_name=block.name, tool_call_id=block.id)
        return message


def Anthropic(*args, **kwargs):
    """返回提供 ``messages.create`` 接口的客户端。"""
    if _use_responses():
        return ResponsesClient(*args, **kwargs)
    from anthropic import Anthropic as _Anthropic
    return _Anthropic(*args, **kwargs)
