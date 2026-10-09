"""权限边界测试：拒绝列表、路径、线程和 MCP 策略。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest


def block(name: str, **tool_input):
    return SimpleNamespace(name=name, input=tool_input, type="tool_use")


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "sudo apt-get update",
        "shutdown -h now",
        "reboot",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
    ],
)
def test_deny_list_blocks_dangerous_commands(host_factory, command) -> None:
    host = host_factory()

    assert "deny list" in host.permission_hook(block("bash", command=command))


def test_permission_matches_substrings_not_whitespace(
    host_factory, approval_hook
) -> None:
    host = host_factory()
    assert approval_hook(host.permission_hook, block("bash", command="echo hello"), "approve") is None
    assert "deny list" in host.permission_hook(
        block("bash", command="echo sudo")
    )


def test_only_approval_releases_the_shell_gate(host_factory, approval_hook) -> None:
    host = host_factory()
    prompt = block("bash", command="echo hi")

    assert approval_hook(host.permission_hook, prompt, "deny") == "Permission denied by user"
    assert approval_hook(host.permission_hook, prompt, "closed") == "Permission denied by user"
    assert approval_hook(host.permission_hook, prompt, "approve") is None


def test_non_string_shell_command_is_rejected(host_factory) -> None:
    host = host_factory()

    assert host.permission_hook(block("bash", command=["echo", "hi"])) == (
        "Permission denied: shell command must be a string"
    )


def test_file_paths_must_stay_inside_the_workspace(host_factory) -> None:
    host = host_factory()

    assert host.permission_hook(block("read_file", path="notes/todo.md")) is None
    assert "outside the workspace" in host.permission_hook(
        block("write_file", path="../escape.md", content="x")
    )
    assert "path must be a string" in host.permission_hook(
        block("edit_file", path=123, old_text="a", new_text="b")
    )


def test_scheduled_turns_cannot_ask_for_approval(host_factory) -> None:
    host = host_factory()
    results = []

    def ask():
        results.append(host.permission_hook(block("bash", command="echo hi")))

    thread = threading.Thread(target=ask)
    thread.start()
    thread.join(timeout=5)

    assert results == [
        "Permission denied: interactive shell approval is unavailable "
        "in this execution context"
    ]


def test_mcp_tools_default_to_confirmation(host_factory, approval_hook) -> None:
    host = host_factory()
    call = block("mcp__deploy__status", service="web")

    assert approval_hook(host.permission_hook, call, "deny") == "Permission denied by user"

    host.connect_mcp("deploy")
    host.assemble_tool_pool()
    assert host.permission_hook(call) is None


def test_hooks_return_the_first_non_none_result(host_factory) -> None:
    host = host_factory()
    seen = []

    host.register_hook("Stop", lambda messages: seen.append("first"))
    host.register_hook("Stop", lambda messages: "stop here")
    host.register_hook("Stop", lambda messages: seen.append("third"))

    assert host.trigger_hooks("Stop", []) == "stop here"
    assert seen == ["first"]


def test_large_output_hook_reports_only_oversized_payloads(
    host_factory, capsys
) -> None:
    host = host_factory()

    host.large_output_hook(block("bash", command="x"), "small")
    host.large_output_hook(block("bash", command="x"), "y" * 100_001)

    output = capsys.readouterr().out
    assert output.count("large output from") == 1


def test_subagent_uses_restricted_tools_and_permission_hook(
    host_factory, tmp_path
) -> None:
    host = host_factory()
    calls = []
    responses = iter([
        SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", id="tool-1", name="write_file",
            input={"path": "../escape.md", "content": "blocked"},
        )]),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="Done")]),
    ])

    def create(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return next(responses)

    host.core.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    assert host.spawn_subagent("inspector", "Inspect only", "once")["text"] == "Done"

    assert {tool["name"] for tool in calls[0]["tools"]} == {
        "bash", "read_file", "write_file", "edit_file", "glob",
    }
    assert "outside the workspace" in calls[1]["messages"][-1]["content"][0][
        "content"]
    assert not (tmp_path.parent / "escape.md").exists()


@pytest.mark.parametrize("owner", ["lead", "child"])
@pytest.mark.parametrize("decision", [None, "", False, 0, "denied"])
def test_tool_gate_uses_non_none_contract_for_lead_and_child(host_factory, monkeypatch, tmp_path, owner, decision):
    host = host_factory()
    posted = []
    monkeypatch.setitem(host.hooks.HOOKS, "PreToolUse", [lambda call: decision])
    monkeypatch.setitem(host.hooks.HOOKS, "PostToolUse", [lambda call, output: posted.append(call.name)])
    tool_call = SimpleNamespace(type="tool_use", id="write", name="write_file",
                                input={"path": "gate.txt", "content": "effect"})
    responses = iter([
        SimpleNamespace(stop_reason="tool_use", content=[tool_call]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done")])])
    histories = []

    def create(**kwargs):
        histories.append(list(kwargs["messages"]))
        return next(responses)

    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    if owner == "lead":
        messages = [{"role": "user", "content": "write"}]
        host.agent_loop(messages, "write")
    else:
        host.spawn_subagent("gate-child", "write", "once")
    assert (tmp_path / "gate.txt").exists() is (decision is None)
    assert posted == (["write_file"] if decision is None else [])
    output = histories[1][-1]["content"][0]["content"]
    assert output == ("Wrote 6 bytes to gate.txt" if decision is None else str(decision))


@pytest.mark.parametrize("decision", ["", False, 0, "denied"])
@pytest.mark.parametrize("background", [False, True])
def test_stop_after_pre_hook_takes_priority_over_denial(host_factory, monkeypatch, tmp_path, decision, background):
    host = host_factory()
    from agent_harness.runtime.interaction import ExecutionContext, InteractionChannel, execution_context
    stop = threading.Event()
    channel = InteractionChannel()
    context = ExecutionContext(channel, "test", 1, "turn", stop_event=stop)
    posted = []

    def pre(call):
        stop.set()
        return decision

    monkeypatch.setitem(host.hooks.HOOKS, "PreToolUse", [pre])
    monkeypatch.setitem(host.hooks.HOOKS, "PostToolUse", [lambda *args: posted.append(True)])
    call = SimpleNamespace(type="tool_use", id="call", name="bash" if background else "write_file",
                           input={"command": "touch must-not-exist", "background": True} if background
                           else {"path": "must-not-exist", "content": "effect"})
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(stop_reason="tool_use", content=[call]))))
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
    assert posted == [] and not host.scheduler.background_tasks
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("owner", ["lead", "child"])
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_tool_hook_exceptions_keep_existing_propagation(host_factory, monkeypatch, tmp_path, owner, event):
    host = host_factory()

    def fail(*args):
        raise RuntimeError("hook failed")

    monkeypatch.setitem(host.hooks.HOOKS, "PreToolUse", [])
    monkeypatch.setitem(host.hooks.HOOKS, "PostToolUse", [])
    monkeypatch.setitem(host.hooks.HOOKS, event, [fail])
    call = SimpleNamespace(type="tool_use", id="write", name="write_file",
                           input={"path": "effect.txt", "content": "effect"})
    if owner == "lead":
        monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(stop_reason="tool_use", content=[call]))))
        with pytest.raises(RuntimeError, match="hook failed"):
            host.agent_loop([{"role": "user", "content": "write"}], "write")
    else:
        from agent_harness.agents.subagent_session import SubagentSession
        session = SubagentSession("child", None, "test", "test", mode="once", task_tools=False)
        with pytest.raises(RuntimeError, match="hook failed"):
            session.execute_tool(call)
    assert (tmp_path / "effect.txt").exists() is (event == "PostToolUse")


def test_background_call_checks_once_and_posts_once_on_completion(host_factory, monkeypatch, tmp_path):
    host = host_factory()
    from test_interactive_runtime import response, tool
    seen = []
    monkeypatch.setitem(host.hooks.HOOKS, "PreToolUse", [lambda call: seen.append("pre")])
    monkeypatch.setitem(host.hooks.HOOKS, "PostToolUse", [lambda call, output: seen.append("post")])
    # Run the real worker synchronously so delivery needs no polling.
    monkeypatch.setattr(host.scheduler.threading, "Thread", lambda target, **kwargs: SimpleNamespace(start=target))
    responses = iter([response(tools=[tool("bash", command="printf done", background=True)], stop="tool_use"),
                      response()])
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: next(responses))))
    host.agent_loop([{"role": "user", "content": "run"}], "run")
    assert seen == ["pre", "post"]


def test_background_dispatch_rechecks_stop_after_selection(host_factory, monkeypatch, tmp_path):
    host = host_factory()
    from test_interactive_runtime import response, tool
    from agent_harness.runtime.interaction import ExecutionContext, InteractionChannel, execution_context
    stop = threading.Event()
    context = ExecutionContext(InteractionChannel(), "session", 1, "turn", stop_event=stop)
    monkeypatch.setitem(host.hooks.HOOKS, "PreToolUse", [])

    def select(*args):
        stop.set()
        return True

    monkeypatch.setattr(host.scheduler, "should_run_background", select)
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: response(tools=[tool("bash", command="touch not-started", background=True)], stop="tool_use"))))
    messages = [{"role": "user", "content": "run"}]
    with execution_context(context):
        outcome = host.agent_loop(messages, "run")
    assert outcome.status == "stopped"
    assert not host.scheduler.background_tasks and not (tmp_path / "not-started").exists()
