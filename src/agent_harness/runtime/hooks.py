"""Hooks 与权限检查。

Hooks 位于工具处理器之外，因此权限、日志和 Stop 行为可独立调整。
工具权限来自已成功装配的 registry 快照；未知 MCP 工具需确认。
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import core
from ..tools import tool_registry
from . import interaction

# -- Hooks 与权限检查 --

# Hooks 放在工具处理器之外，主循环可增加权限、日志和停止逻辑而不修改各个工具。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    """按顺序登记某个事件的回调。

    输入：event 为 HOOKS 中的事件名；callback 为接受该事件参数的函数。
    返回：None。将回调追加到事件列表，不去重；未知事件名抛出 KeyError。
    """
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """按注册顺序执行事件回调，并在第一个非 None 结果处停止。

    输入：event 为事件名；args 为原样传给各回调的位置参数。
    返回：第一个非 None 的回调结果；全部放行时返回 None。
        回调异常继续抛出。工具执行入口将拒绝结果转为工具结果文本。
    """
    for callback in HOOKS[event]:
        name = getattr(callback, "__qualname__", type(callback).__name__)
        operation = interaction.operation_status("hook", name, "running", event=event)
        try:
            result = callback(*args)
        except Exception as exc:
            interaction.operation_status("hook", name, "failed", operation_id=operation,
                                         event=event, reason=f"{type(exc).__name__}: {exc}")
            raise
        state = "denied" if event == "PreToolUse" and result is not None else "completed"
        interaction.operation_status("hook", name, state, operation_id=operation,
                                     event=event, **({"reason": str(result)} if result is not None else {}))
        if result is not None:
            return result
    return None


@dataclass(frozen=True)
class ToolCheck:
    status: str
    text: str = ""


def check_tool_use(block, *, authorize=None, stopped=None):
    """Return allowed/denied/stopped after authorization and the pre-use hooks."""
    with interaction.operation_context(tool_call_id=getattr(block, "id", None)):
        if authorize is not None:
            denied = authorize(block)
            if denied is not None:
                return ToolCheck("denied", str(denied))
        denied = trigger_hooks("PreToolUse", block)
        if stopped is not None and stopped():
            return ToolCheck("stopped", "Interrupted before execution")
        if denied is not None:
            return ToolCheck("denied", str(denied))
        return ToolCheck("allowed")


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]


def _confirm(block, kind):
    context = interaction.current_context()
    if context is None or context.origin != "frontend":
        return f"Permission denied: interactive {kind} approval is unavailable in this execution context"
    decision = context.channel.request_approval(
        context, block.name, getattr(block, "id", ""), block.input)
    return None if decision == "approve" else "Permission denied by user"


def permission_hook(block):
    """在工具执行前检查命令、文件路径和 MCP 工具权限。

    输入：block 为工具请求，包含 name 和 input。
    返回：放行时为 None，拒绝时为原因字符串。
        Shell and unknown MCP approvals require explicit frontend context.
        File paths must remain within the startup workspace.
        任务目录的进一步限制由文件工具执行器检查。
    """
    # 权限层在分发前检查原始 tool_use，可拒绝、询问用户或放行。
    if block.name == "bash":
        command = block.input.get("command", "")
        if not isinstance(command, str):
            return "Permission denied: shell command must be a string"
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied: '{pattern}' is on the deny list"
        return _confirm(block, "shell")
    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not isinstance(path, str):
            return "Permission denied: path must be a string"
        if not (core.WORKDIR / path).resolve().is_relative_to(core.WORKDIR):
            return "Permission denied: path is outside the workspace"
    if (block.name.startswith("mcp__")
            and tool_registry.REGISTRY.policy_for(block.name) != "allow"):
        return _confirm(block, "MCP")
    return None


def log_hook(block):
    if interaction.current_context() is None:
        core.terminal_print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def large_output_hook(block, output):
    if interaction.current_context() is None and len(str(output)) > core.LARGE_OUTPUT_WARNING_CHARS:
        core.terminal_print(f"\033[33m[HOOK] large output from {block.name}: "
              f"{len(str(output))} chars\033[0m")
    return None


def user_prompt_hook(query: str):
    if interaction.current_context() is None:
        core.terminal_print(f"\033[90m[HOOK] UserPromptSubmit: {core.WORKDIR}\033[0m")
    return None


def stop_hook(messages: list):
    if interaction.current_context() is not None:
        return None
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            tool_count += sum(1 for item in content
                              if isinstance(item, dict)
                              and item.get("type") == "tool_result")
    core.terminal_print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None


register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)
