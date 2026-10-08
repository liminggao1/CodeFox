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
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    monkeypatch.setattr(host.CONSOLE, "ask", lambda prompt=None: "y")

    assert host.permission_hook(block("bash", command="echo hello")) is None
    assert "deny list" in host.permission_hook(
        block("bash", command="echo sudo")
    )


def test_only_yes_releases_the_shell_gate(host_factory, monkeypatch) -> None:
    host = host_factory()
    prompt = block("bash", command="echo hi")

    for answer in ("", "no", "n", "N", "yesss"):
        monkeypatch.setattr(host.CONSOLE, "ask", lambda prompt=None, a=answer: a)
        assert host.permission_hook(prompt) == "Permission denied by user"

    for answer in ("y", "Y", "yes", "YES"):
        monkeypatch.setattr(host.CONSOLE, "ask", lambda prompt=None, a=answer: a)
        assert host.permission_hook(prompt) is None


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
        "during an asynchronous turn"
    ]


def test_mcp_tools_default_to_confirmation(host_factory, monkeypatch) -> None:
    host = host_factory()
    call = block("mcp__deploy__status", service="web")

    monkeypatch.setattr(host.CONSOLE, "ask", lambda prompt=None: "no")
    assert host.permission_hook(call) == "Permission denied by user"

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
