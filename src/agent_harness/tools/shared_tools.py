"""Shared tool definitions and actor binding, without capability selection."""

from copy import deepcopy
from functools import partial

from ..workspace import tasks
from . import file_tools
from .tool_registry import ToolDefinition


FILE_TOOL_NAMES = ("bash", "read_file", "write_file", "edit_file", "glob")
TASK_TOOL_NAMES = ("list_tasks", "claim_task", "complete_task")

# -- Common schemas and the two existing model-facing surfaces --

_SCHEMAS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
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
    {"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

_CHILD_DESCRIPTIONS = {
    "read_file": "Read file.",
    "write_file": "Write file.",
    "edit_file": "Replace text in a file.",
    "glob": "Find files by glob pattern; ** matches recursively.",
    "list_tasks": "List all tasks on the board.",
    "complete_task": "Mark an in-progress task as completed.",
}

# -- Task board text and actor binding --

def run_list_tasks() -> str:
    """Return the existing board text without modifying tasks."""
    task_list = tasks.list_tasks()
    if not task_list:
        return "No tasks."
    return "\n".join(
        f"  {task.id}: {task.subject} [{task.status}]"
        + (f" (wt:{task.worktree})" if task.worktree else "")
        for task in task_list)


def _run_task(action, task_id, owner, missing_label):
    try:
        return action(task_id, owner=owner)
    except ValueError as exc:
        return f"Error: {exc}"
    except FileNotFoundError:
        return f"Error: {missing_label} {task_id} not found"


def run_claim_task(task_id: str, *, owner="agent", missing_label="task") -> str:
    """Claim using the supplied task owner and preserve the caller's error text."""
    return _run_task(tasks.claim_task, task_id, owner, missing_label)


def run_complete_task(task_id: str, *, owner="agent", missing_label="task") -> str:
    """Complete using the supplied task owner; tasks retains all validation."""
    return _run_task(tasks.complete_task, task_id, owner, missing_label)


def shared_tool_definitions(owner: str, *, profile: str) -> dict[str, ToolDefinition]:
    """Build fresh schemas and handlers; callers select their available tools.

    Input: task owner and the existing lead/child schema surface.
    Return: eight definitions with independent nested schema data. No execution.
    """
    if profile not in {"lead", "child"}:
        raise ValueError(f"Unknown shared tool profile: {profile}")
    lead = profile == "lead"
    handlers = file_tools.bind_file_tools(owner, allow_background=lead)
    missing_label = "task" if lead else "Task"
    handlers.update({
        "list_tasks": run_list_tasks,
        "claim_task": partial(run_claim_task, owner=owner, missing_label=missing_label),
        "complete_task": partial(run_complete_task, owner=owner, missing_label=missing_label),
    })
    definitions = {}
    for schema in deepcopy(_SCHEMAS):
        name = schema["name"]
        if lead and name == "bash":
            schema["input_schema"]["properties"]["run_in_background"] = {"type": "boolean"}
        if not lead and name in _CHILD_DESCRIPTIONS:
            schema["description"] = _CHILD_DESCRIPTIONS[name]
        origin = f"built-in tool '{name}'" if lead else f"child tool '{name}'"
        definitions[name] = ToolDefinition(schema, handlers[name], origin)
    return definitions
