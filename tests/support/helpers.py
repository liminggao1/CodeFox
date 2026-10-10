"""各测试共用的运行时事件与会话文件工具。"""

from __future__ import annotations

import json
import time


def until(runtime, event_type, *, submission_id=None, timeout=5):
    """读取事件直到出现目标类型，返回该事件和途经事件。"""
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = runtime.channel.next_event(timeout=0.05)
        if event is not None:
            events.append(event)
            runtime.channel.acknowledge_event(event.sequence)
            if event.type == event_type and (submission_id is None or event.payload.get("submission_id") == submission_id):
                return event, events
    raise AssertionError(f"Missing {event_type}: {events}")


def displayed_approval(runtime, timeout=3):
    """离线适配器先展示前置通知，再读取审批请求。"""
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = runtime.channel.next_event(timeout=0.01)
        if event is not None:
            events.append(event)
            runtime.channel.acknowledge_event(event.sequence)
        request = runtime.channel.next_approval()
        if request is not None:
            return request, events
    raise AssertionError(f"Missing approval: {events}")


def stored(runtime, session_id=None):
    """读取会话文件中的原始 JSON。"""
    path = runtime.session.store.path(session_id or runtime.session.session_id)
    return json.loads(path.read_text(encoding="utf-8"))


def records(snapshot, event_type=None):
    """从会话快照里取出展示记录，可按类型过滤。"""
    values = [item for item in snapshot["events"] if item.get("kind") == "display"]
    return values if event_type is None else [item for item in values if item["type"] == event_type]


def wait_stored(runtime, predicate, timeout=3):
    """轮询会话文件直到满足条件。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = stored(runtime)
        if predicate(value):
            return value
        time.sleep(0.02)
    raise AssertionError(f"Conversation record not saved: {stored(runtime)}")


def complete(runtime, text):
    """提交一次输入并等待回合结束。"""
    submission = runtime.submit(text)
    assert submission.status == "accepted"
    return until(runtime, "turn_finished", submission_id=submission.submission_id)
