"""Offline adapter tests exercise the same delivery seam as CLI and TUI."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from agent_harness.runtime.interaction import (
    ExecutionContext, InteractionChannel, current_context, execution_context,
)


def frontend(channel, turn_id="turn-1"):
    return ExecutionContext(channel, "session-1", 1, turn_id,
                            origin="frontend", stop_event=threading.Event())


def test_concurrent_events_have_delivery_order_and_detached_payloads():
    channel = InteractionChannel()
    context = frontend(channel)
    original = {"items": [{"text": "中文"}]}
    published = context.publish("message", **original)
    original["items"][0]["text"] = "mutated"
    published.payload["items"][0]["text"] = "publisher mutation"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: context.publish("progress", number=n), range(40)))
    events = [channel.next_event() for _ in range(41)]
    assert [event.sequence for event in events] == list(range(1, 42))
    assert events[0].payload == {"items": [{"text": "中文"}]}
    assert all(event.session_id == "session-1" for event in events)
    assert channel.next_event() is None
    with pytest.raises(TypeError):
        context.publish("invalid", live_object=threading.Event())


def test_context_is_scoped_and_independent_threads_do_not_inherit():
    context = frontend(InteractionChannel())
    assert current_context() is None
    with execution_context(context):
        assert current_context() is context
        with execution_context(context.delegate("once-child")):
            assert current_context().origin == "frontend"
            assert current_context().source == "once-child"
        assert current_context() is context
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(current_context).result(timeout=2) is None
    assert current_context() is None


@pytest.mark.parametrize("event,result,failure,state", [
    ("UserPromptSubmit", None, False, "完成"),
    ("PreToolUse", "拒绝原因", False, "拒绝"),
    ("PostToolUse", None, True, "失败"),
    ("Stop", "继续工作", False, "完成"),
])
def test_hook_lifecycle_is_logged_without_display_status(monkeypatch, event, result, failure, state):
    from agent_harness.runtime import hooks

    channel = InteractionChannel()
    calls = []

    def check(value):
        calls.append(value)
        if failure:
            raise RuntimeError("hook failed")
        return result

    def following(value):
        raise AssertionError("Callbacks after a result or failure must not run")

    callbacks = [check] if result is None and not failure else [check, following]
    monkeypatch.setitem(hooks.HOOKS, event, callbacks)
    with execution_context(frontend(channel)):
        if failure:
            with pytest.raises(RuntimeError, match="hook failed"):
                hooks.trigger_hooks(event, "中文输入")
        else:
            assert hooks.trigger_hooks(event, "中文输入") == result
    assert calls == ["中文输入"]
    events = [channel.next_event(), channel.next_event()]
    assert all(item is not None and item.type == "log" for item in events)
    assert channel.next_event() is None
    assert all(f"[HOOK] {event} / " in item.payload["text"] and "check" in item.payload["text"]
               for item in events)
    assert "开始" in events[0].payload["text"]
    assert state in events[1].payload["text"] and "耗时" in events[1].payload["text"]
    if result is not None:
        assert result in events[1].payload["text"]
    if failure:
        assert "RuntimeError: hook failed" in events[1].payload["text"]


def test_runtime_notifications_have_no_session_or_approval_capability():
    channel = InteractionChannel()
    context = ExecutionContext(channel, None, 0, source="cron", scope="runtime")
    context.publish("log", text="Runtime service error")
    event = channel.next_event()
    assert event.scope == "runtime" and event.session_id is None and event.turn_id is None
    assert event.generation == 0
    invalid_frontend = replace(context, origin="frontend", turn_id="turn-1")
    assert channel.request_approval(invalid_frontend, "bash", "tool-1", {}) == "deny"
    assert channel.next_approval() is None


@pytest.mark.parametrize("origin", ["background", "persistent", "workflow"])
def test_noninteractive_origins_never_queue_approval(origin):
    channel = InteractionChannel()
    context = replace(frontend(channel), origin=origin)
    assert channel.request_approval(context, "bash", "tool-1", {}) == "deny"
    assert channel.next_approval() is None


def test_offline_adapter_responds_to_exact_request_once():
    channel = InteractionChannel()
    context = frontend(channel)
    arguments = {"command": "echo 中文"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(channel.request_approval, context, "bash", "tool-1", arguments)
        request = channel.next_approval(timeout=2)
        assert request is not None
        assert request.tool_call_id == "tool-1"
        assert request.arguments == arguments
        arguments["command"] = "changed"
        assert request.arguments["command"] == "echo 中文"
        assert not channel.respond("unknown", 1, "approve")
        assert not channel.respond(request.request_id, 2, "approve")
        assert not waiting.done()
        assert channel.respond(request.request_id, 1, "approve")
        assert waiting.result(timeout=2) == "approve"
        assert not channel.respond(request.request_id, 1, "approve")


def test_approval_notifies_turn_phase_observer():
    channel = InteractionChannel()
    phases = []
    context = replace(frontend(channel), phase_observer=phases.append)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(channel.request_approval, context, "bash", "tool-1", {})
        request = channel.next_approval(timeout=2)
        assert request is not None
        assert phases == ["waiting_approval"]
        assert channel.respond(request.request_id, request.generation, "approve")
        assert waiting.result(timeout=2) == "approve"
    assert phases == ["waiting_approval", "executing_tools"]


def test_approval_waits_for_display_not_dequeue_or_later_notifications():
    channel = InteractionChannel()
    context = frontend(channel)
    context.publish("tool_call", name="bash")
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            waiting = pool.submit(channel.request_approval, context, "bash", "tool-1", {})
            assert channel.next_approval(timeout=0.05) is None
            event = channel.next_event()
            assert channel.next_approval(timeout=0.05) is None
            context.publish("log", text="Later notification")
            assert not channel.acknowledge_event(event.sequence + 1)
            assert channel.acknowledge_event(event.sequence)
            request = channel.next_approval(timeout=2)
            assert request is not None
            assert channel.respond(request.request_id, request.generation, "approve")
            assert waiting.result(timeout=2) == "approve"
        finally:
            channel.close()


def test_simultaneous_requests_cannot_release_each_other():
    channel = InteractionChannel()
    context = frontend(channel)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waits = {name: pool.submit(channel.request_approval, context.delegate(name),
                                   "bash", name, {}) for name in ("a", "b")}
        requests = [channel.next_approval(timeout=2) for _ in waits]
        request_by_source = {request.source: request for request in requests}
        channel.respond(request_by_source["b"].request_id, 1, "deny")
        assert waits["b"].result(timeout=2) == "deny"
        assert not waits["a"].done()
        channel.respond(request_by_source["a"].request_id, 1, "approve")
        assert waits["a"].result(timeout=2) == "approve"


@pytest.mark.parametrize("operation, expected", [("stop", "deny"), ("close", "closed")])
def test_stop_and_close_release_pending_and_reject_new_requests(operation, expected):
    channel = InteractionChannel()
    context = frontend(channel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(channel.request_approval, context, "bash", "tool-1", {})
        request = channel.next_approval(timeout=2)
        assert request is not None
        if operation == "stop":
            context.stop_event.set()
            channel.cancel_turn(context.turn_id)
        else:
            channel.close()
        assert waiting.result(timeout=2) == expected
        assert not channel.respond(request.request_id, 1, "approve")
        assert channel.request_approval(context, "bash", "tool-2", {}) == expected


def test_close_drains_terminal_notification_and_rejects_late_publishers():
    channel = InteractionChannel()
    context = frontend(channel)
    context.publish("runtime_closed", saved=False)
    channel.close()
    assert channel.next_event().type == "runtime_closed"
    assert channel.next_event(timeout=1) is None
    with pytest.raises(RuntimeError, match="closed"):
        context.publish("late_message", text="late")


@pytest.mark.parametrize("operation,expected", [("stop", "deny"), ("close", "closed"), ("switch", "deny")])
def test_unshown_event_cannot_trap_cancelled_or_stale_approval(operation, expected):
    channel = InteractionChannel()
    context = frontend(channel)
    context.publish("tool_call", name="bash")
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            waiting = pool.submit(channel.request_approval, context, "bash", "old", {})
            assert channel.next_approval(timeout=0.05) is None
            if operation == "stop":
                channel.cancel_turn(context.turn_id)
            elif operation == "close":
                channel.close()
            else:
                current = replace(context, session_id="session-2", generation=2, turn_id="new-turn")
                current.publish("session_changed")
            assert waiting.result(timeout=2) == expected
            assert channel.next_approval() is None
            if operation == "switch":
                stale = channel.next_event()
                assert stale.type == "session_changed"
                assert channel.acknowledge_event(stale.sequence)
                context.publish("log", text="old session")
                cron = replace(context, scope="runtime", generation=0)
                cron.publish("log", text="runtime survives")
                event = channel.next_event()
                assert event.payload["text"] == "runtime survives"
                assert channel.acknowledge_event(event.sequence)
                new = pool.submit(channel.request_approval, current, "bash", "new", {})
                request = channel.next_approval(timeout=2)
                assert request is not None and request.session_id == "session-2"
                assert channel.respond(request.request_id, 2, "approve")
                assert new.result(timeout=2) == "approve"
        finally:
            channel.close()


def test_confirmation_cannot_skip_earlier_delivered_events():
    channel = InteractionChannel()
    context = frontend(channel)
    context.publish("assistant_message", text="First")
    context.publish("tool_call", name="bash")
    first, second = channel.next_event(), channel.next_event()
    assert not channel.acknowledge_event(second.sequence)
    assert channel.acknowledge_event(first.sequence)
    assert channel.acknowledge_event(second.sequence)
