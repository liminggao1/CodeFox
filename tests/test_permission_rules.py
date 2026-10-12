"""默认规则、单次路径授权与持久权限模式的行为测试。"""

import json
from types import SimpleNamespace

import pytest

from agent_harness.runtime import hooks, permissions
from agent_harness.tools import file_tools
from support.fakes import response, tool
from support.helpers import displayed_approval, until, stored


def call(name, **args):
    return SimpleNamespace(name=name, input=args, id="test-call")


@pytest.mark.parametrize("command", ["pwd", "ls -la", "ls -lh 中文目录", "cat '中文 文件.txt'",
    "git status", "git status --short", "git diff", "git diff --stat", "git diff --cached -- a.txt"])
def test_read_only_commands_never_request_approval(host_factory, monkeypatch, command):
    host_factory()
    monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
    assert hooks.permission_hook(call("bash", command=command)) is None


@pytest.mark.parametrize("command", ["ls > files.txt", "ls && touch a.txt", "cat a | cat",
    "ls\npwd", "ls $HOME", "cat $(pwd)/a", "cat `pwd`/a", "python script.py",
    "ls --unknown", "git -c alias.x=push x", "git diff --ext-diff", "git diff --textconv",
    "env ls", "bash -c 'ls'", "ls < a.txt", "ls; pwd"])
def test_complex_or_unknown_commands_need_single_approval(host_factory, approval_hook, command):
    host_factory()
    assert approval_hook(hooks.permission_hook, call("bash", command=command), "deny") == "Permission denied by user"


@pytest.mark.parametrize("command", ["/usr/bin/sudo ls", "ls; shutdown -h now", "pwd && reboot",
    "rm -fr /", "rm --recursive --force /", "dd if=/dev/zero of=a", "env sudo ls",
    "bash -c 'sudo ls'", "mkfs.ext4 /dev/sda", "LANG=C sudo ls"])
@pytest.mark.parametrize("mode", ["default", "no_approval"])
def test_denied_commands_remain_denied(host_factory, command, mode):
    host_factory()
    permissions.SETTINGS.mode = mode
    assert "deny list" in hooks.permission_hook(call("bash", command=command))


@pytest.mark.parametrize("command", ["echo sudo", "cat sudo.txt", "cat 'shutdown reboot mkfs'",
    "echo 'x; sudo ls'", "echo ';' sudo", "echo 'first\nsudo ls'"])
def test_printed_words_and_paths_are_not_denied(host_factory, command):
    host_factory()
    assert permissions.evaluate(call("bash", command=command), mode="default").action != "deny"


@pytest.mark.parametrize("mode", ["default", "no_approval"])
@pytest.mark.parametrize("name,args", [("bash", {"command": 12}),
    ("bash", {"command": "ls", "run_in_background": "yes"}),
    ("write_file", {"path": "a", "content": []}),
    ("edit_file", {"path": 12, "old_text": "a", "new_text": "b"}),
    ("read_file", {"path": "a", "offset": True})])
def test_invalid_arguments_fail_without_approval(host_factory, monkeypatch, mode, name, args):
    host_factory()
    permissions.SETTINGS.mode = mode
    monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
    result = hooks.check_tool_use(call(name, **args))
    assert result.status == "failed" and "Invalid arguments" in result.text


@pytest.mark.parametrize("name", ["read_file", "write_file", "edit_file"])
def test_outside_path_is_approved_once_and_executes(host_factory, approval_hook, tmp_path, name):
    host_factory()
    outside = tmp_path.parent / (tmp_path.name + "-中文 文件.txt")
    outside.write_text("before", encoding="utf-8")
    args = {"path": str(outside)}
    if name == "write_file":
        args["content"] = "after"
    elif name == "edit_file":
        args.update(old_text="before", new_text="after")
    block = call(name, **args)
    handler = {"read_file": file_tools.run_read, "write_file": file_tools.run_write,
               "edit_file": file_tools.run_edit}[name]
    def execute(request):
        decision = hooks.permission_hook(request)
        return decision, handler(**request.input) if decision is None else None
    try:
        result = approval_hook(execute, block, "approve")
        assert result[0] is None and result[1].status == "success"
        assert outside.read_text(encoding="utf-8") == ("before" if name == "read_file" else "after")
        assert approval_hook(hooks.permission_hook, call(name, **args), "deny") == "Permission denied by user"
    finally:
        outside.unlink(missing_ok=True)


def test_mode_persistence_and_failed_save_preserve_previous_setting(host_factory, monkeypatch):
    host_factory()
    permissions.SETTINGS.save("no_approval")
    other = permissions.PermissionSettings()
    other.load()
    assert other.mode == "no_approval"
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(permissions.os, "replace", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        permissions.SETTINGS.save("default")
    assert permissions.SETTINGS.mode == "no_approval"
    assert json.loads(permissions.settings_path().read_text())["permission_mode"] == "no_approval"
    assert other.mode == "no_approval"


@pytest.mark.parametrize("text", ["broken", "[]", '{"permission_mode":"unknown"}', '{"permission_mode":[]}'])
def test_invalid_settings_are_reported_and_preserved(text):
    path = permissions.settings_path()
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        permissions.SETTINGS.load()
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("mode", ["default", "no_approval"])
def test_background_approval_availability_and_mcp_policy(host_factory, monkeypatch, mode):
    host_factory()
    permissions.SETTINGS.mode = mode
    assert hooks.permission_hook(call("bash", command="ls -la")) is None
    write = hooks.permission_hook(call("write_file", path="background.txt", content="done"))
    assert write is None
    monkeypatch.setattr(permissions.tool_registry.REGISTRY, "policy_for", lambda name: "deny")
    assert "Permission denied" in hooks.permission_hook(call("mcp__test__write"))
    monkeypatch.setattr(permissions.tool_registry.REGISTRY, "policy_for", lambda name: "confirm")
    result = hooks.permission_hook(call("mcp__test__write"))
    assert result is None if mode == "no_approval" else "approval is unavailable" in result


def test_settings_instances_only_load_at_startup(host_factory):
    host_factory()
    other = permissions.PermissionSettings()
    other.load()
    permissions.SETTINGS.save("no_approval")
    assert other.mode == "default"
    restarted = permissions.PermissionSettings()
    restarted.load()
    assert restarted.mode == "no_approval"


def test_missing_file_is_an_execution_error_after_permission_check(host_factory, monkeypatch):
    host_factory()
    monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
    block = call("read_file", path="missing.txt")
    assert hooks.check_tool_use(block).status == "allowed"
    result = file_tools.run_read(**block.input)
    assert result.status == "failed" and "No such file" in result.content


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


@pytest.mark.parametrize("name", ["read_file", "write_file", "edit_file"])
def test_workspace_file_tools_execute_without_approval(host_factory, monkeypatch, tmp_path, name):
    host_factory()
    target = tmp_path / "中文文件.txt"
    target.write_text("before", encoding="utf-8")
    args = {"path": target.name}
    if name == "write_file":
        args["content"] = "after"
    elif name == "edit_file":
        args.update(old_text="before", new_text="after")
    monkeypatch.setattr(hooks, "_confirm", lambda *args: pytest.fail("unexpected approval"))
    block = call(name, **args)
    assert hooks.check_tool_use(block).status == "allowed"
    assert block.input["path"] == str(target)
    handler = {"read_file": file_tools.run_read, "write_file": file_tools.run_write,
               "edit_file": file_tools.run_edit}[name]
    assert handler(**block.input).status == "success"
    assert target.read_text() == ("before" if name == "read_file" else "after")


def test_default_mode_creates_and_edits_workspace_file_without_approval(runtime_factory, tmp_path):
    runtime, _ = runtime_factory([
        response(tools=[tool("write_file", path="new.txt", content="before")], stop="tool_use"),
        response(tools=[tool("edit_file", "edit", path="new.txt", old_text="before", new_text="after")], stop="tool_use"),
        response("完成")])
    submission = runtime.submit("创建并编辑文件")
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert finished.payload["summary"]["approvals"] == 0
    assert not any(event.type == "approval_requested" for event in events)
    assert (tmp_path / "new.txt").read_text() == "after"


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
