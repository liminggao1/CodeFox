"""工具权限接线：审批、hook、停止优先级与后台执行。"""

from __future__ import annotations

import codefox.runtime.hooks as hooks
import codefox.core as core
import codefox.agents.subagent as subagent_runtime
import codefox.runtime.scheduler as scheduler


import threading
from types import SimpleNamespace

import pytest

from support.fakes import fake_stream_for, response, tool


def test_hooks_return_the_first_non_none_result(host_factory) -> None:
    host = host_factory()
    seen = []

    hooks.register_hook("Stop", lambda messages: seen.append("first"))
    hooks.register_hook("Stop", lambda messages: "stop here")
    hooks.register_hook("Stop", lambda messages: seen.append("third"))

    assert hooks.trigger_hooks("Stop", []) == "stop here"
    assert seen == ["first"]


def test_large_output_hook_reports_only_oversized_payloads(
    host_factory, capsys
) -> None:
    host = host_factory()

    hooks.large_output_hook(tool("bash", command="x"), "small")
    hooks.large_output_hook(tool("bash", command="x"), "y" * 100_001)

    output = capsys.readouterr().out
    assert output.count("large output from") == 1


@pytest.mark.parametrize("owner,decision", [("lead", None), ("lead", ""), ("child", False)])
def test_tool_gate_uses_non_none_contract_for_lead_and_child(host_factory, monkeypatch, tmp_path, owner, decision):
    host = host_factory()
    posted = []
    monkeypatch.setitem(hooks.HOOKS, "PreToolUse", [lambda call: decision])
    monkeypatch.setitem(hooks.HOOKS, "PostToolUse", [lambda call, output: posted.append(call.name)])
    tool_call = SimpleNamespace(type="tool_use", id="write", name="write_file",
                                input={"path": "gate.txt", "content": "effect"})
    responses = iter([
        SimpleNamespace(stop_reason="tool_use", content=[tool_call]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done")])])
    histories = []

    def create(**kwargs):
        histories.append(list(kwargs["messages"]))
        return next(responses)

    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    if owner == "lead":
        messages = [{"role": "user", "content": "write"}]
        host.agent_loop(messages, "write")
    else:
        subagent_runtime.spawn_subagent("gate-child", "write", "once", description="Write file")
    assert (tmp_path / "gate.txt").exists() is (decision is None)
    assert posted == (["write_file"] if decision is None else [])
    output = histories[1][-1]["content"][0]["content"]
    assert output == ("Wrote 6 bytes to gate.txt" if decision is None else str(decision))


@pytest.mark.parametrize("background,decision", [(False, ""), (True, False)])
def test_stop_after_pre_hook_takes_priority_over_denial(host_factory, monkeypatch, tmp_path, decision, background):
    host = host_factory()
    from codefox.runtime.interaction import ExecutionContext, InteractionChannel, execution_context
    stop = threading.Event()
    channel = InteractionChannel()
    context = ExecutionContext(channel, "test", 1, "turn", stop_event=stop)
    posted = []

    def pre(call):
        stop.set()
        return decision

    monkeypatch.setitem(hooks.HOOKS, "PreToolUse", [pre])
    monkeypatch.setitem(hooks.HOOKS, "PostToolUse", [lambda *args: posted.append(True)])
    call = SimpleNamespace(type="tool_use", id="call", name="bash" if background else "write_file",
                           input={"command": "touch must-not-exist", "background": True} if background
                           else {"path": "must-not-exist", "content": "effect"})
    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(stop_reason="tool_use", content=[call]))))
    core.client.messages.stream = fake_stream_for(core.client.messages.create)
    messages = [{"role": "user", "content": "work"}]
    with execution_context(context):
        outcome = host.agent_loop(messages, "work")
    events = []
    while (event := channel.next_event()) is not None:
        events.append(event)
    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0].payload["status"] == "stopped"
    assert results[0].payload["content"] == "Interrupted before execution"
    assert results[0].payload["is_error"] is True
    assert outcome.status == "stopped"
    assert posted == [] and not scheduler._background_tasks
    assert not (tmp_path / "must-not-exist").exists()


def test_background_call_checks_once_and_posts_once_on_completion(host_factory, monkeypatch, tmp_path):
    host = host_factory()
    seen = []
    monkeypatch.setitem(hooks.HOOKS, "PreToolUse", [lambda call: seen.append("pre")])
    monkeypatch.setitem(hooks.HOOKS, "PostToolUse", [lambda call, output: seen.append("post")])
    # Run the real worker synchronously so delivery needs no polling.
    monkeypatch.setattr(scheduler.threading, "Thread", lambda target, **kwargs: SimpleNamespace(start=target))
    responses = iter([response(tools=[tool("bash", command="printf done", background=True)], stop="tool_use"),
                      response()])
    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: next(responses))))
    host.agent_loop([{"role": "user", "content": "run"}], "run")
    assert seen == ["pre", "post"]


def test_background_dispatch_rechecks_stop_after_selection(host_factory, monkeypatch, tmp_path):
    host = host_factory()
    from codefox.runtime.interaction import ExecutionContext, InteractionChannel, execution_context
    stop = threading.Event()
    context = ExecutionContext(InteractionChannel(), "session", 1, "turn", stop_event=stop)
    monkeypatch.setitem(hooks.HOOKS, "PreToolUse", [])

    def select(*args):
        stop.set()
        return True

    monkeypatch.setattr(scheduler, "should_run_background", select)
    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: response(tools=[tool("bash", command="touch not-started", background=True)], stop="tool_use"))))
    core.client.messages.stream = fake_stream_for(core.client.messages.create)
    messages = [{"role": "user", "content": "run"}]
    with execution_context(context):
        outcome = host.agent_loop(messages, "run")
    assert outcome.status == "stopped"
    assert not scheduler._background_tasks and not (tmp_path / "not-started").exists()
