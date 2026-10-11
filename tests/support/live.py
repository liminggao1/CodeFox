"""真实 API 测试共用的 InteractiveRuntime 驱动。"""

from __future__ import annotations

import time


def run_submission(runtime, text: str, *, timeout: float = 180,
                   approve_tools: bool = True, on_event=None,
                   terminal_types=("turn_finished", "command_result")):
    """提交一轮真实请求，确认流式事件、审批和终态均可消费。"""
    submission = runtime.submit(text)
    if submission.status != "accepted":
        raise AssertionError(f"live submission rejected: {submission}")

    events = []
    approvals = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = runtime.channel.next_event(timeout=0.2)
        if event is not None:
            events.append(event)
            runtime.channel.acknowledge_event(event.sequence)
            if on_event is not None:
                on_event(runtime, event)
            if (event.type in set(terminal_types)
                    and event.payload.get("submission_id") == submission.submission_id):
                return submission, event, events, approvals

        request = runtime.channel.next_approval(timeout=0)
        if request is not None:
            decision = "approve" if approve_tools else "deny"
            approvals.append((request.tool_name, decision))
            if not runtime.channel.respond(request.request_id, request.generation, decision):
                raise AssertionError("live approval response was rejected")

    raise AssertionError(
        f"live submission timed out: {text!r}; "
        f"last events={[event.type for event in events[-12:]]}")


def drain_runtime_closed(runtime, timeout: float = 180) -> bool:
    """关闭真实运行时并等待其终态事件。"""
    runtime.close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = runtime.channel.next_event(timeout=0.2)
        if event is None:
            continue
        runtime.channel.acknowledge_event(event.sequence)
        if event.type == "runtime_closed":
            runtime._thread.join(timeout=timeout)
            return True
    runtime._thread.join(timeout=1)
    return False
