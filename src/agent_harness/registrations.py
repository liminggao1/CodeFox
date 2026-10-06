"""工具 schema 与处理器映射表。

工具名称与 Python 实现统一在这里映射。除反向依赖本模块的 mcp_tools 外，
其余导入都保持在模块级；这一处循环依赖在 assemble_tool_pool 中延迟处理。
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

# -- Lead 的 worktree 工具 --

def run_create_worktree(name: str, task_id: str) -> str:
    return task_worktrees.create_worktree(name, task_id)

# -- 基础工具处理器 --

def run_create_task(subject: str, description: str = "") -> str:
    task = tasks.create_task(subject, description)
    print(f"  \033[34m[create] {task.subject}\033[0m")
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
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
    tasks = tasks.list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    try:
        return tasks.get_task_json(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    try:
        return tasks.claim_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_complete_task(task_id: str) -> str:
    try:
        return tasks.complete_task(task_id, owner="agent")
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

def run_spawn_teammate(name: str, role: str, prompt: str,
                       task_id: str | None = None,
                       require_plan: bool = False) -> str:
    return teams.spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    with teams.team_lock:
        if not teams.active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}"
            for name, status in sorted(teams.active_teammates.items())
        )


def run_send_message(to: str, content: str) -> str:
    if to not in teams.active_teammates:
        return f"Teammate '{to}' is not active"
    teams.BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_connect_mcp(name: str) -> str:
    return mcp_tools.connect_mcp(name)


# -- 工具定义 --

# 模型看到工具 schema，Python 执行处理器。两张表明确列出新增的每项能力。
BUILTIN_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "run_in_background": {"type": "boolean"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "todo_write",
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
    {"name": "task",
     "description": "Launch a focused subagent. Returns only its final summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "load_skill",
     "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
    {"name": "create_task",
     "description": "Create a task and return its runtime-generated ID.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"}},
                      "required": ["subject"],
                      "additionalProperties": False}},
    {"name": "update_task",
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
    {"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "schedule_cron",
     "description": ("Schedule a cron job. cron is 5-field: min hour dom "
                     "month dow. For one-shot reminders, compute the target "
                     "minute and set recurring=false."),
     "input_schema": {"type": "object",
                      "properties": {"cron": {"type": "string"},
                                     "prompt": {"type": "string"},
                                     "recurring": {"type": "boolean"},
                                     "durable": {"type": "boolean"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "List registered cron jobs.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate.",
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
    {"name": "list_teammates", "description": "List active teammates.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "send_message", "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
    {"name": "create_worktree",
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
    {"name": "connect_mcp",
     "description": "Connect to an MCP server (docs, deploy) and discover tools.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
]

BUILTIN_HANDLERS = {
    "bash": file_tools.run_agent_bash,
    "read_file": file_tools.run_agent_read,
    "write_file": file_tools.run_agent_write,
    "edit_file": file_tools.run_agent_edit,
    "glob": file_tools.run_agent_glob,
    "todo_write": file_tools.run_todo_write, "task": subagent.spawn_subagent,
    "load_skill": skills.load_skill,
    "create_task": run_create_task, "update_task": run_update_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "schedule_cron": scheduler.run_schedule_cron,
    "list_crons": scheduler.run_list_crons,
    "cancel_cron": scheduler.run_cancel_cron,
    "spawn_teammate": run_spawn_teammate,
    "list_teammates": run_list_teammates,
    "send_message": run_send_message,
    "request_shutdown": teams.run_request_shutdown,
    "request_plan": teams.run_request_plan, "review_plan": teams.run_review_plan,
    "create_worktree": run_create_worktree,
    "connect_mcp": run_connect_mcp,
}
