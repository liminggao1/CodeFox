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
import os
import urllib.error
import urllib.request

__all__ = ["Anthropic"]

DEFAULT_TIMEOUT = 300.0
USER_AGENT = "learncc-llm-client/1.0"


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
            items.append({
                "type": "function_call_output",
                "call_id": _field(block, "tool_use_id"),
                "output": _text_of(_field(block, "content", "")),
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

def _parse_arguments(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stop_reason(response: dict, has_tool_call: bool) -> str:
    if has_tool_call:
        return "tool_use"
    details = response.get("incomplete_details") or {}
    if (response.get("status") == "incomplete"
            and details.get("reason") == "max_output_tokens"):
        return "max_tokens"
    return "end_turn"


def _to_message(response: dict, fallback_model) -> Message:
    blocks = []
    has_tool_call = False
    for item in response.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    blocks.append(Block(type="text", text=part.get("text", "")))
        elif kind == "function_call":
            has_tool_call = True
            blocks.append(Block(
                type="tool_use",
                id=item.get("call_id") or item.get("id"),
                name=item.get("name"),
                input=_parse_arguments(item.get("arguments")),
            ))
    usage = response.get("usage") or {}
    return Message(
        content=blocks,
        stop_reason=_stop_reason(response, has_tool_call),
        usage=Block(input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0)),
        model=response.get("model") or fallback_model,
        id=response.get("id"),
    )


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
        return body[:500] or str(error.reason or "request failed")
    detail = parsed.get("error", parsed)
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
    return str(detail)[:1000]


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
                f"Invalid JSON from {self._endpoint()}: {body[:500]}") from None

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

def Anthropic(*args, **kwargs):
    """返回提供 ``messages.create`` 接口的客户端。"""
    if _use_responses():
        return ResponsesClient(*args, **kwargs)
    from anthropic import Anthropic as _Anthropic
    return _Anthropic(*args, **kwargs)
