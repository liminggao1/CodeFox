"""工具 schema 与处理器映射表。

组合 Lead 专有和共用工具，共用定义由 shared_tools 维护；装配由 tool_registry 负责。
"""

from __future__ import annotations

from .tools.tool_result import ToolResult
from . import core

import json

from .workspace import tasks
from .workspace import task_worktrees
from .context import skills
from .agents import subagent
from .tools import file_tools
from .tools import shared_tools
from .runtime import scheduler
from .tools import mcp_tools
from .tools.tool_registry import ToolDefinition

# -- Lead 的 worktree 工具 --

def run_create_worktree(name: str, task_id: str) -> ToolResult:
    """处理主模型发出的 ``create_worktree`` 工具请求。

    输入：
        name: 新工作目录的名称，也用于分支 ``wt/<name>``。
        task_id: 要绑定的待办任务 ID，必须在任务被认领前创建绑定。
    返回：
        ``task_worktrees.create_worktree`` 返回的创建结果或错误文本。
    """
    try:
        return ToolResult(task_worktrees.create_worktree(name, task_id), "success")
    except ValueError as exc:
        return ToolResult(str(exc), "failed")


def run_remove_worktree(name: str) -> ToolResult:
    """处理 Lead 的非强制 worktree 清理请求。

    输入：name 为已注册且绑定任务的 worktree 名称。
    返回：移除结果或拒绝原因；成功时保留分支，释放 Lead 自己的已完成分配。
        不接受丢弃改动或释放子 agent分配的参数。
    """
    try:
        return ToolResult(task_worktrees.remove_worktree(name), "success")
    except ValueError as exc:
        return ToolResult(str(exc), "failed")

# -- 基础工具处理器 --

def run_create_task(subject: str, description: str = "") -> ToolResult:
    """处理 ``create_task`` 请求，将生成的任务 ID 返回给模型。

    输入：
        subject: 非空任务标题。
        description: 可选的任务描述。
    返回：
        包含已保存待办任务 ID 的确认文本。
        校验和存储异常继续抛给通用工具执行器处理。
    """
    task = tasks.create_task(subject, description)
    core.terminal_print(f"  \033[34m[create] {task.subject}\033[0m")
    return ToolResult(f"Created {task.id}: {task.subject}", "success")


def run_update_task(task_id: str, addBlockedBy: list[str]) -> ToolResult:
    """处理模型为尚未认领的任务添加依赖的工具请求。

    输入：
        task_id: 要更新的待办任务 ID。
        addBlockedBy: 必须先完成的现有任务 ID 列表。
    返回：
        列出依赖的确认文本；输入不合法或任务缺失时返回错误文本。
        更新成功时会保存任务记录。
    """
    try:
        task = tasks.update_task(task_id, addBlockedBy)
    except ValueError as exc:
        return ToolResult(f"Error: {exc}", "failed")
    except FileNotFoundError:
        return ToolResult(f"Error: Task {task_id} not found", "failed")
    dependencies = ", ".join(task.blockedBy) or "(none)"
    core.terminal_print(f"  \033[34m[update] {task.subject} blockedBy: {dependencies}\033[0m")
    return ToolResult(f"Updated {task.id} blockedBy: {dependencies}", "success")


def run_list_tasks() -> ToolResult:
    """Return the shared task board text for the Lead."""
    return shared_tools.run_list_tasks()


def run_get_task(task_id: str) -> ToolResult:
    """处理模型读取任务完整字段的工具请求。

    输入：
        task_id: 要查看的任务 ID。
    返回：
        格式化的任务 JSON；记录不合法或不存在时返回错误文本。
        不修改任务或分配信息。
    """
    try:
        return ToolResult(tasks.get_task_json(task_id), "success")
    except ValueError as exc:
        return ToolResult(f"Error: {exc}", "failed")
    except FileNotFoundError:
        return ToolResult(f"Error: task {task_id} not found", "failed")

def run_claim_task(task_id: str) -> ToolResult:
    """Claim with Lead task ownership and the existing error text."""
    return shared_tools.run_claim_task(task_id)


def run_complete_task(task_id: str) -> ToolResult:
    """Complete with Lead task ownership; tasks retains validation."""
    return shared_tools.run_complete_task(task_id)


def run_spawn_subagent(name: str, prompt: str, mode: str, role: str = "coder",
                       task_id: str | None = None, require_plan: bool = False, tools=None,
                       *, description: str,
                       max_rounds: int | None = None, max_tool_calls: int | None = None) -> ToolResult:
    """Serialize the unified runtime result for the Lead's creation tool."""
    result = subagent.spawn_subagent(
        name=name, prompt=prompt, mode=mode, role=role, description=description,
        task_id=task_id, require_plan=require_plan, tools=tools,
        max_rounds=max_rounds, max_tool_calls=max_tool_calls)
    status = {"completed": "success", "started": "started",
              "budget_exhausted": "failed", "network_error": "failed",
              "stopped": "stopped",
              "failed": "failed"}[result["status"]]
    return ToolResult(json.dumps(result, ensure_ascii=False), status)


def run_list_subagents() -> ToolResult:
    """List active names, roles, modes and states without returning history."""
    return ToolResult(json.dumps(subagent.list_subagents(), ensure_ascii=False), "success")


def run_send_message(to: str, content: str) -> ToolResult:
    """Deliver the Lead's chosen message to a live persistent child."""
    return ToolResult(subagent.send_message("lead", to, content), "success")


def run_get_subagent_output(agent_id: str, block: bool = True,
                            timeout: float = 30) -> ToolResult:
    """Return a child task status/result without exposing its full history."""
    try:
        result = subagent.get_subagent_output(agent_id, block=block, timeout=timeout)
    except ValueError as exc:
        return ToolResult(f"Error: {exc}", "failed")
    return ToolResult(json.dumps(result, ensure_ascii=False), "success")


def run_connect_mcp(name: str) -> ToolResult:
    """处理主模型连接模拟 MCP 服务器的请求。

    输入：name 为内置模拟服务器名称。
    返回：连接结果或错误文本；成功后工具在下一次装配时可见。
    """
    return ToolResult(mcp_tools.connect_mcp(name), "success")


# -- 工具定义 --

# Model schemas and handlers share definitions; preserve the Lead's order.
_SHARED = shared_tools.shared_tool_definitions("agent", profile="lead")
BUILTIN_DEFINITIONS = [
    _SHARED["bash"],
    _SHARED["read_file"],
    _SHARED["write_file"],
    _SHARED["edit_file"],
    _SHARED["glob"],
    ToolDefinition({"name": "todo_write",
     "description": "Create and manage a task list for the current session.",
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array",
                          "items": {"type": "object",
                                    "properties": {
                                        "content": {"type": "string"},
                                        "status": {"type": "string",
                                                   "enum": ["pending", "in_progress", "completed"]}},
                                    "required": ["content", "status"]}}},
                      "required": ["todos"]}},
                   file_tools.run_todo_write, "built-in tool 'todo_write'"),
    ToolDefinition({"name": "load_skill",
     "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
                   lambda name: ToolResult(skills.load_skill(name)), "built-in tool 'load_skill'"),
    ToolDefinition({"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
                   None, "built-in tool 'compact'"),
    ToolDefinition({"name": "create_task",
     "description": "Create a task and return its runtime-generated ID.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"}},
                      "required": ["subject"],
                      "additionalProperties": False}},
                   run_create_task, "built-in tool 'create_task'"),
    ToolDefinition({"name": "update_task",
     "description": "Add dependencies using IDs returned by create_task.",
     "input_schema": {"type": "object",
                      "properties": {
                          "task_id": {"type": "string",
                                      "pattern": "^task_[0-9a-f]{8}$"},
                          "addBlockedBy": {
                              "type": "array",
                              "items": {"type": "string",
                                        "pattern": "^task_[0-9a-f]{8}$"},
                              "minItems": 1}},
                      "required": ["task_id", "addBlockedBy"],
                      "additionalProperties": False}},
                   run_update_task, "built-in tool 'update_task'"),
    _SHARED["list_tasks"],
    ToolDefinition({"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
                   run_get_task, "built-in tool 'get_task'"),
    _SHARED["claim_task"],
    _SHARED["complete_task"],
    ToolDefinition({"name": "schedule_cron",
     "description": ("Schedule a cron job. cron is 5-field: min hour dom "
                     "month dow. For one-shot reminders, compute the target "
                     "minute and set recurring=false."),
     "input_schema": {"type": "object",
                      "properties": {"cron": {"type": "string"},
                                     "prompt": {"type": "string"},
                                     "recurring": {"type": "boolean"},
                                     "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
                   scheduler.run_schedule_cron, "built-in tool 'schedule_cron'"),
    ToolDefinition({"name": "list_crons", "description": "List registered cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
                   scheduler.run_list_crons, "built-in tool 'list_crons'"),
    ToolDefinition({"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
                   scheduler.run_cancel_cron, "built-in tool 'cancel_cron'"),
    ToolDefinition({"name": "spawn_subagent",
     "description": ("Create a child with explicit once or persistent lifetime. "
                     "once waits for its final result; persistent returns started "
                     "and delivers later events. Only persistent supports messaging and plan approval."),
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                          "description": {"type": "string", "minLength": 1,
                                          "description": "A short 3-5 word task description."},
                          "prompt": {"type": "string", "minLength": 1},
                          "mode": {"type": "string", "enum": ["once", "persistent"]},
                          "role": {"type": "string"},
                          "task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                          "require_plan": {"type": "boolean"},
                          "tools": {"type": "array", "items": {"type": "string"},
                                    "description": "Optional tool whitelist; omit for mode defaults, [] for no tools."},
                          "max_rounds": {"type": "integer", "minimum": 1},
                          "max_tool_calls": {"type": "integer", "minimum": 1}},
                      "required": ["name", "description", "prompt", "mode"],
                      "additionalProperties": False}},
                   run_spawn_subagent, "built-in tool 'spawn_subagent'"),
    ToolDefinition({"name": "list_subagents", "description": "List active child instances, modes and states.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
                   run_list_subagents, "built-in tool 'list_subagents'"),
    ToolDefinition({"name": "get_subagent_output",
     "description": "Get a child status, partial result, error and usage by agent ID.",
     "input_schema": {"type": "object",
                      "properties": {"agent_id": {"type": "string"},
                                     "block": {"type": "boolean"},
                                     "timeout": {"type": "number", "minimum": 0, "maximum": 60,
                                                 "description": "Wait at most 60 seconds; a timeout preserves the child's status."}},
                      "required": ["agent_id"], "additionalProperties": False}},
                   run_get_subagent_output, "built-in tool 'get_subagent_output'"),
    ToolDefinition({"name": "send_message", "description": "Send message to a persistent subagent.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
                   run_send_message, "built-in tool 'send_message'"),
    ToolDefinition({"name": "request_shutdown",
     "description": "Request a persistent subagent to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
                   subagent.run_request_shutdown, "built-in tool 'request_shutdown'"),
    ToolDefinition({"name": "request_plan",
     "description": "Ask a persistent subagent to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["name", "task"]}},
                   subagent.run_request_plan, "built-in tool 'request_plan'"),
    ToolDefinition({"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
                   subagent.run_review_plan, "built-in tool 'review_plan'"),
    ToolDefinition({"name": "create_worktree",
     "description": "Create a task-bound git worktree for a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"name": {
                                         "type": "string",
                                         "pattern": ("^(?!.*\\.\\.)[A-Za-z0-9]"
                                                     "[A-Za-z0-9._-]{0,63}$"),
                                         "maxLength": 64,
                                     },
                                     "task_id": {"type": "string"}},
                      "required": ["name", "task_id"],
                      "additionalProperties": False}},
                   run_create_worktree, "built-in tool 'create_worktree'"),
    ToolDefinition({"name": "remove_worktree",
     "description": ("Remove a completed task's clean worktree and retain its branch. "
                     "Does not merge changes. Refuses persistent subagent leases, running background "
                     "commands, and uncommitted or ignored files. On success, releases "
                     "any matching completed Lead assignment and restores its default cwd."),
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"],
                      "additionalProperties": False}},
                   run_remove_worktree, "built-in tool 'remove_worktree'"),
    ToolDefinition({"name": "connect_mcp",
     "description": "Connect to an MCP server (docs, deploy) and discover tools.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
                   run_connect_mcp, "built-in tool 'connect_mcp'"),
]

BUILTIN_TOOLS = [entry.schema for entry in BUILTIN_DEFINITIONS]
BUILTIN_HANDLERS = {
    entry.schema["name"]: entry.handler
    for entry in BUILTIN_DEFINITIONS if entry.handler is not None
}


def builtin_definitions() -> list[ToolDefinition]:
    """提供内置工具的统一定义，供注册器装配。

    返回：保持声明顺序的新列表，共用工具含独立 schema；不执行处理器。
    """
    shared = shared_tools.shared_tool_definitions("agent", profile="lead")
    return [shared.get(entry.schema["name"], entry) for entry in BUILTIN_DEFINITIONS]
