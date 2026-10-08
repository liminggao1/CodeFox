"""工具 schema 与处理器映射表。

工具名称与 Python 实现统一在这里映射，装配由 tool_registry 负责。
"""

from __future__ import annotations

from . import tasks
from . import task_worktrees
from . import teams
from . import skills
from . import subagent
from . import file_tools
from . import scheduler
from . import mcp_tools
from .tool_registry import ToolDefinition

# -- Lead 的 worktree 工具 --

def run_create_worktree(name: str, task_id: str) -> str:
    """处理主模型发出的 ``create_worktree`` 工具请求。

    输入：
        name: 新工作目录的名称，也用于分支 ``wt/<name>``。
        task_id: 要绑定的待办任务 ID，必须在任务被认领前创建绑定。
    返回：
        ``task_worktrees.create_worktree`` 返回的创建结果或错误文本。
    """
    return task_worktrees.create_worktree(name, task_id)


def run_remove_worktree(name: str) -> str:
    """处理 Lead 的非强制 worktree 清理请求。

    输入：name 为已注册且绑定任务的 worktree 名称。
    返回：移除结果或拒绝原因；成功时保留分支，释放 Lead 自己的已完成分配。
        不接受丢弃改动或释放队友分配的参数。
    """
    return task_worktrees.remove_worktree(name)

# -- 基础工具处理器 --

def run_create_task(subject: str, description: str = "") -> str:
    """处理 ``create_task`` 请求，将生成的任务 ID 返回给模型。

    输入：
        subject: 非空任务标题。
        description: 可选的任务描述。
    返回：
        包含已保存待办任务 ID 的确认文本。
        校验和存储异常继续抛给通用工具执行器处理。
    """
    task = tasks.create_task(subject, description)
    print(f"  \033[34m[create] {task.subject}\033[0m")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
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
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"
    dependencies = ", ".join(task.blockedBy) or "(none)"
    print(f"  \033[34m[update] {task.subject} blockedBy: {dependencies}\033[0m")
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks() -> str:
    """为主模型的 ``list_tasks`` 请求生成任务板文本。

    返回：
        每个任务一行，包含 ID、标题、状态和可选的 worktree 名称。
        无任务时返回 ``'No tasks.'``，不修改任务记录。
    """
    task_list = tasks.list_tasks()
    if not task_list:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in task_list)


def run_get_task(task_id: str) -> str:
    """处理模型读取任务完整字段的工具请求。

    输入：
        task_id: 要查看的任务 ID。
    返回：
        格式化的任务 JSON；记录不合法或不存在时返回错误文本。
        不修改任务或分配信息。
    """
    try:
        return tasks.get_task_json(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    """以主 agent 的身份 ``'agent'`` 处理 ``claim_task`` 请求。

    输入：
        task_id: 主 agent 要认领的任务 ID。
    返回：
        成功或拒绝原因的字符串；任务不合法或缺失时转为错误文本。
        成功时保存任务归属，并登记工具工作目录。
    """
    try:
        return tasks.claim_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_complete_task(task_id: str) -> str:
    """以主 agent 的身份 ``'agent'`` 处理 ``complete_task`` 请求。

    输入：
        task_id: 主 agent 正在处理的任务 ID。
    返回：
        完成结果或拒绝原因；任务不合法或缺失时转为错误文本。
        成功时保存 ``completed``，分配记录在工作回合结束时另行释放。
    """
    try:
        return tasks.complete_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_spawn_teammate(name: str, role: str, prompt: str,
                       task_id: str | None = None,
                       require_plan: bool = False) -> str:
    """处理主模型创建队友的工具请求。

    输入：name 为队友名称；role 为角色；prompt 为初始指令；task_id 为可选初始任务；
        require_plan 为是否要求先获得计划批准。
    返回：队友启动或失败提示；由团队模块登记状态并启动守护线程。
    """
    return teams.spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    """展示当前活跃队友及其运行状态。

    返回：按名称排序的状态文本；无活跃队友时返回提示，不改变队友状态。
    """
    with teams.team_lock:
        if not teams.active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}"
            for name, status in sorted(teams.active_teammates.items())
        )


def run_send_message(to: str, content: str) -> str:
    """以 Lead 身份向活跃队友发送普通消息。

    输入：to 为队友名称；content 为消息正文。
    返回：投递确认或收件人不活跃的提示；成功时追加到队友信箱。
    """
    if to not in teams.active_teammates:
        return f"Teammate '{to}' is not active"
    teams.BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_connect_mcp(name: str) -> str:
    """处理主模型连接模拟 MCP 服务器的请求。

    输入：name 为内置模拟服务器名称。
    返回：连接结果或错误文本；成功后工具在下一次装配时可见。
    """
    return mcp_tools.connect_mcp(name)


# -- 工具定义 --

# 模型看到工具 schema，Python 执行处理器。两张表明确列出新增的每项能力。
BUILTIN_DEFINITIONS = [
    ToolDefinition({"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
                   file_tools.run_agent_bash, "built-in tool 'bash'"),
    ToolDefinition({"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
                   file_tools.run_agent_read, "built-in tool 'read_file'"),
    ToolDefinition({"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
                   file_tools.run_agent_write, "built-in tool 'write_file'"),
    ToolDefinition({"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
                   file_tools.run_agent_edit, "built-in tool 'edit_file'"),
    ToolDefinition({"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
                   file_tools.run_agent_glob, "built-in tool 'glob'"),
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
    ToolDefinition({"name": "task",
     "description": "Launch a focused subagent. Returns only its final summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
                   subagent.spawn_subagent, "built-in tool 'task'"),
    ToolDefinition({"name": "load_skill",
     "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
                   skills.load_skill, "built-in tool 'load_skill'"),
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
    ToolDefinition({"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
                   run_list_tasks, "built-in tool 'list_tasks'"),
    ToolDefinition({"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
                   run_get_task, "built-in tool 'get_task'"),
    ToolDefinition({"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
                   run_claim_task, "built-in tool 'claim_task'"),
    ToolDefinition({"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
                   run_complete_task, "built-in tool 'complete_task'"),
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
    ToolDefinition({"name": "spawn_teammate", "description": "Spawn a persistent teammate.",
     "input_schema": {"type": "object",
                      "properties": {"name": {
                                         "type": "string",
                                         "pattern": "^[A-Za-z0-9_-]{1,64}$",
                                     },
                                     "role": {"type": "string"},
                                     "prompt": {"type": "string"},
                                     "task_id": {
                                         "type": "string",
                                         "pattern": "^task_[0-9a-f]{8}$",
                                     },
                                     "require_plan": {"type": "boolean"}},
                      "required": ["name", "role", "prompt"]}},
                   run_spawn_teammate, "built-in tool 'spawn_teammate'"),
    ToolDefinition({"name": "list_teammates", "description": "List active teammates.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
                   run_list_teammates, "built-in tool 'list_teammates'"),
    ToolDefinition({"name": "send_message", "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
                   run_send_message, "built-in tool 'send_message'"),
    ToolDefinition({"name": "request_shutdown",
     "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
                   teams.run_request_shutdown, "built-in tool 'request_shutdown'"),
    ToolDefinition({"name": "request_plan",
     "description": "Ask a teammate to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
                   teams.run_request_plan, "built-in tool 'request_plan'"),
    ToolDefinition({"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
                   teams.run_review_plan, "built-in tool 'review_plan'"),
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
                     "Does not merge changes. Refuses teammate leases, running background "
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

    返回：保持声明顺序的新列表，元素为已有 ToolDefinition，不执行工具处理器。
    """
    return list(BUILTIN_DEFINITIONS)
