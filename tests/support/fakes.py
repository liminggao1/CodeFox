"""各测试共用的假模型响应构造器。"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace


def response(text="Done", *, tools=(), stop="end_turn", usage=(10, 3)):
    """构造文本和工具响应；text=None 只含工具，usage 为 (input, output) 或 None。"""
    content = [] if text is None else [text_block(text)]
    value = blocks(*content, *tools, stop_reason=stop)
    if usage is not None:
        value.usage = SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
    return value


def tool(tool_name, call_id="call", **arguments):
    """构造模型响应中的工具调用块。"""
    return SimpleNamespace(type="tool_use", name=tool_name, id=call_id, input=arguments)


def tool_call(name, **arguments):
    """构造带唯一 id 的工具调用块，同一响应包含多个调用时使用。"""
    return tool(name, f"{name}-{time.monotonic_ns()}", **arguments)


def text_block(text):
    """构造模型响应中的文本块。"""
    return SimpleNamespace(type="text", text=text)


def blocks(*content, stop_reason="end_turn"):
    """按调用方给出的块列表构造模型响应，不带 usage。"""
    return SimpleNamespace(content=list(content), stop_reason=stop_reason)


def blocking_client(started, release, text="done", timeout=10):
    """构造阻塞在 release 上的假 client；进入时置位 started，便于用例观察忙碌态。"""
    def create(**kwargs):
        started.set()
        assert release.wait(timeout)
        return response(text)
    return create


def fake_stream_for(client):
    """把同步 client 包装成流式接口，供共享驱动消费。"""
    @contextmanager
    def stream(**kwargs):
        value = client(**kwargs)
        events = []
        for index, block in enumerate(value.content):
            if block.type == "text":
                events.extend([SimpleNamespace(type="content_block_start", index=index, content_block=block),
                               SimpleNamespace(type="content_block_delta", index=index,
                                               delta=SimpleNamespace(type="text_delta", text=block.text)),
                               SimpleNamespace(type="content_block_stop", index=index)])
        events.append(SimpleNamespace(type="message_stop"))
        class Stream:
            def __iter__(self):
                return iter(events)
            def get_final_message(self):
                return value
        yield Stream()
    return stream
