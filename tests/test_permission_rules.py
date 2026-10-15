"""默认规则、单次路径授权与持久权限模式的行为测试。"""

import json

import pytest

from codefox import core
from codefox.runtime import hooks, permissions
from codefox.tools import file_tools
from support.fakes import response, tool
from support.helpers import displayed_approval, until, stored


def test_read_only_commands_never_request_approval(host_factory, monkeypatch):
    host_factory()
    monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
    for command in ("pwd", "ls -lh 中文目录", "cat '中文 文件.txt'",
                    "git status --short", "git diff --cached -- a.txt"):
        assert hooks.permission_hook(tool("bash", command=command)) is None, command


@pytest.mark.parametrize("command", ["cat $(pwd)/a", "git diff --ext-diff"])
def test_complex_or_unknown_commands_need_single_approval(host_factory, approval_hook, command):
    host_factory()
    assert approval_hook(hooks.permission_hook, tool("bash", command=command), "deny") == "Permission denied by user"
    assert approval_hook(hooks.permission_hook, tool("bash", command=command), "closed") == "Permission denied by user"
    assert approval_hook(hooks.permission_hook, tool("bash", command=command), "approve") is None


def test_denied_commands_remain_denied(host_factory):
    host_factory()
    for command in ("sudo apt-get update", "shutdown -h now", "reboot",
                    "/usr/bin/sudo ls", "pwd && reboot", "rm -fr /",
                    "rm --recursive --force /", "dd if=/dev/zero of=a",
                    "env sudo ls", "bash -c 'sudo ls'", "mkfs.ext4 /dev/sda",
                    "LANG=C sudo ls"):
        for mode in ("default", "no_approval"):
            permissions.SETTINGS.mode = mode
            assert "deny list" in hooks.permission_hook(tool("bash", command=command)), (mode, command)


def test_printed_words_and_paths_are_not_denied(host_factory):
    host_factory()
    for command in (
        "echo sudo", "cat sudo.txt", "cat 'shutdown reboot mkfs'",
        "echo 'x; sudo ls'", "echo ';' sudo", "echo 'first\nsudo ls'",
    ):
        assert permissions.evaluate(tool("bash", command=command), mode="default").action != "deny", command


def test_invalid_arguments_fail_without_approval(host_factory, monkeypatch):
    host_factory()
    monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
    for mode in ("default", "no_approval"):
        for name, args in [("bash", {"command": 12}),
                           ("bash", {"command": "ls", "run_in_background": "yes"}),
                           ("write_file", {"path": "a", "content": []}),
                           ("edit_file", {"path": 12, "old_text": "a", "new_text": "b"}),
                           ("read_file", {"path": "a", "offset": True})]:
            permissions.SETTINGS.mode = mode
            result = hooks.check_tool_use(tool(name, **args))
            assert result.status == "failed" and "Invalid arguments" in result.text, (mode, name, args)


@pytest.mark.parametrize("name,outside", [("read_file", False), ("write_file", True), ("edit_file", True)])
def test_file_tools_require_single_approval_only_outside_workspace(
    host_factory, approval_hook, tmp_path, monkeypatch, name, outside,
):
    host_factory()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(core, "WORKDIR", workspace)
    monkeypatch.chdir(workspace)
    target = (tmp_path if outside else workspace) / "中文 文件.txt"
    target.write_text("before", encoding="utf-8")
    args = {"path": f"../{target.name}" if outside else target.name}
    if name == "write_file":
        args["content"] = "after"
    elif name == "edit_file":
        args.update(old_text="before", new_text="after")
    block = tool(name, **args)
    handler = {"read_file": file_tools.run_read, "write_file": file_tools.run_write,
               "edit_file": file_tools.run_edit}[name]
    def execute(request):
        decision = hooks.check_tool_use(request)
        return decision, handler(**request.input) if decision.status == "allowed" else None
    if outside:
        result = approval_hook(execute, block, "approve")
        assert approval_hook(hooks.permission_hook, tool(name, **args), "deny") == "Permission denied by user"
    else:
        monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
        result = execute(block)
    assert result[0].status == "allowed" and result[1].status == "success"
    assert block.input["path"] == str(target)
    assert target.read_text(encoding="utf-8") == ("before" if name == "read_file" else "after")


def test_mode_persistence_and_failed_save_preserve_previous_setting(host_factory, monkeypatch):
    host_factory()
    other = permissions.PermissionSettings()
    other.load()
    permissions.SETTINGS.save("no_approval")
    assert other.mode == "default"
    restarted = permissions.PermissionSettings()
    restarted.load()
    assert restarted.mode == "no_approval"
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(permissions.os, "replace", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        permissions.SETTINGS.save("default")
    assert permissions.SETTINGS.mode == "no_approval"
    assert json.loads(permissions.settings_path().read_text())["permission_mode"] == "no_approval"
    assert other.mode == "default"


def test_invalid_settings_are_reported_and_preserved():
    path = permissions.settings_path()
    for text in ("broken", "[]", '{"permission_mode":"unknown"}', '{"permission_mode":[]}'):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            permissions.SETTINGS.load()
        assert path.read_text(encoding="utf-8") == text, text


@pytest.mark.parametrize("mode", ["default", "no_approval"])
def test_background_approval_availability_and_mcp_policy(host_factory, monkeypatch, mode):
    host_factory()
    permissions.SETTINGS.mode = mode
    assert hooks.permission_hook(tool("bash", command="ls -la")) is None
    write = hooks.permission_hook(tool("write_file", path="background.txt", content="done"))
    assert write is None
    monkeypatch.setattr(permissions.tool_registry.REGISTRY, "policy_for", lambda name: "deny")
    assert "Permission denied" in hooks.permission_hook(tool("mcp__test__write"))
    monkeypatch.setattr(permissions.tool_registry.REGISTRY, "policy_for", lambda name: "confirm")
    result = hooks.permission_hook(tool("mcp__test__write"))
    assert result is None if mode == "no_approval" else "approval is unavailable" in result


def test_runtime_write_approval_records_same_result_for_model_and_user(runtime_factory, tmp_path):
    target = tmp_path.parent / (tmp_path.name + "-approved.txt")
    runtime, _ = runtime_factory([response(tools=[tool("write_file", path=str(target), content="done")], stop="tool_use"), response()])
    submitted = runtime.submit("write")
    request, _ = displayed_approval(runtime)
    assert request.tool_name == "write_file"
    assert request.arguments["path"] == str(target)
    with pytest.raises(ValueError, match="不能切换"):
        runtime.set_permission_mode("no_approval")
    runtime.channel.respond(request.request_id, request.generation, "approve")
    until(runtime, "turn_finished", submission_id=submitted.submission_id)
    snapshot = stored(runtime)
    result = next(message for message in snapshot["messages"] if message["role"] == "user" and isinstance(message["content"], list))["content"][0]
    display = next(record for record in snapshot["events"] if record["type"] == "tool_result")
    assert display["payload"]["content"] == result["content"]
    assert target.read_text() == "done"
    target.unlink()


def test_no_approval_mode_executes_outside_path_and_invalid_input_still_fails(runtime_factory, tmp_path):
    target = tmp_path.parent / (tmp_path.name + "-outside.txt")
    runtime, _ = runtime_factory([response(tools=[tool("write_file", path=str(target), content="done"),
        tool("write_file", "invalid", path="bad.txt", content=12)], stop="tool_use"), response()])
    runtime.set_permission_mode("no_approval")
    try:
        submitted = runtime.submit("write")
        _, events = until(runtime, "turn_finished", submission_id=submitted.submission_id)
        assert not any(event.type == "approval_requested" for event in events)
        assert target.read_text() == "done" and not (tmp_path / "bad.txt").exists()
        results = [event for event in events if event.type == "display_record"
                   and event.payload["record"]["type"] == "tool_result"]
        assert any(record["payload"]["status"] == "failed" for record in
                   (event.payload["record"] for event in results))
    finally:
        target.unlink(missing_ok=True)
