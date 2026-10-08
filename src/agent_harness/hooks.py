"""Hooks 与权限检查。

Hooks 位于工具处理器之外，因此权限、日志和 Stop 行为可独立调整。
工具权限来自已成功装配的 registry 快照；未知 MCP 工具需确认。
"""

from __future__ import annotations

import threading

from . import core
from . import tool_registry

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
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
def permission_hook(block):
    """在工具执行前检查命令、文件路径和 MCP 工具权限。

    输入：block 为工具请求，包含 name 和 input。
    返回：放行时为 None，拒绝时为原因字符串。
        bash 先按子串检查拒绝列表，再在主线程询问用户；文件路径必须在启动目录内。
        未明确允许的 MCP 工具也需主线程确认；其他线程无法交互确认，因此拒绝。
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
        if threading.current_thread() is not threading.main_thread():
            return ("Permission denied: interactive shell approval is unavailable "
                    "during an asynchronous turn")
        core.terminal_print("\n\033[33m[permission] shell command\033[0m")
        core.terminal_print(f"  {command}")
        choice = core.CONSOLE.ask("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            return "Permission denied by user"
    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not isinstance(path, str):
            return "Permission denied: path must be a string"
        if not (core.WORKDIR / path).resolve().is_relative_to(core.WORKDIR):
            return "Permission denied: path is outside the workspace"
    if (block.name.startswith("mcp__")
            and tool_registry.REGISTRY.policy_for(block.name) != "allow"):
        if threading.current_thread() is not threading.main_thread():
            return ("Permission denied: interactive MCP approval is unavailable "
                    "during an asynchronous turn")
        core.terminal_print(f"\n\033[33m[permission] MCP tool: {block.name}\033[0m")
        choice = core.CONSOLE.ask("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            return "Permission denied by user"
    return None


def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] large output from {block.name}: "
              f"{len(str(output))} chars\033[0m")
    return None


def user_prompt_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: {core.WORKDIR}\033[0m")
    return None


def stop_hook(messages: list):
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            tool_count += sum(1 for item in content
                              if isinstance(item, dict)
                              and item.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None


register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)
