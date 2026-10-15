"""Shared tool definitions and actor binding, without capability selection."""

from .tool_result import ToolResult
from copy import deepcopy
from functools import partial

from ..workspace import tasks
from . import file_tools
from .tool_registry import ToolDefinition


FILE_TOOL_NAMES = ("bash", "read_file", "write_file", "edit_file", "glob")
TASK_TOOL_NAMES = ("list_tasks", "claim_task", "complete_task")

# -- Common schemas and the two existing model-facing surfaces --

_SCHEMAS = [
    {"name": "bash", "description":
     "Run shell commands for content search with rg/grep, filtering or combining "
     "results, tests, builds, git, pipelines, and operations requiring shell semantics. "
     "When a dedicated tool is relevant, prefer it: use read_file for known files, "
     "glob for file discovery, edit_file for targeted edits, and write_file for "
     "creating or replacing complete files. Use bash when the dedicated tools cannot "
     "express the operation.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description":
     "Read a known file or a specific line range. Prefer this over bash cat, head, "
     "tail, or sed for ordinary file reading.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description":
     "Create a file or replace its complete contents. Prefer this over shell "
     "redirection, echo, or heredoc when the operation is a direct file write.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description":
     "Replace exact text in an existing file once. Prefer this for targeted edits "
     "instead of sed or awk.",
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
    "bash": "Run shell commands for content search with rg/grep, tests, git or other shell operations. Prefer an available dedicated tool when it can express the operation.",
    "read_file": "Read an original project file. offset skips lines from 0; limit bounds the number of lines returned. Prefer a focused range over repeatedly reading the complete file. Internal session tool-result archives cannot be read with this tool.",
    "write_file": "Create or completely replace a file; prefer over shell redirection, echo, or heredoc.",
    "edit_file": "Replace text in an existing file; prefer over sed or awk for targeted edits.",
    "glob": "Find files by glob pattern; prefer over find or ls for file discovery. ** matches recursively.",
    "list_tasks": "List all tasks on the board.",
    "claim_task": "Claim a pending task before working on it.",
    "complete_task": "Mark an in-progress task as completed.",
}

_CHILD_COORDINATION_SCHEMAS = {
    "send_message": {
        "name": "send_message",
        "description": "Send an intermediate message to 'lead' or an active persistent subagent.",
        "input_schema": {"type": "object", "properties": {
            "to": {"type": "string"}, "content": {"type": "string"}},
            "required": ["to", "content"]}},
    "submit_plan": {
        "name": "submit_plan",
        "description": "Submit a plan for Lead approval.",
        "input_schema": {"type": "object", "properties": {
            "plan": {"type": "string"}}, "required": ["plan"]}},
}

# -- Task board text and actor binding --

def run_list_tasks() -> ToolResult:
    """Return the existing board text without modifying tasks."""
    task_list = tasks.list_tasks()
    if not task_list:
        return ToolResult("No tasks.")
    return ToolResult("\n".join(
        f"  {task.id}: {task.subject} [{task.status}]"
        + (f" (wt:{task.worktree})" if task.worktree else "")
        for task in task_list))


def _run_task(action, task_id, owner, missing_label):
    try:
        return ToolResult(action(task_id, owner=owner), "success")
    except ValueError as exc:
        return ToolResult(f"Error: {exc}", "failed")
    except FileNotFoundError:
        return ToolResult(f"Error: {missing_label} {task_id} not found", "failed")


def run_claim_task(task_id: str, *, owner="agent", missing_label="task") -> ToolResult:
    """Claim using the supplied task owner and preserve the caller's error text."""
    return _run_task(tasks.claim_task, task_id, owner, missing_label)


def run_complete_task(task_id: str, *, owner="agent", missing_label="task") -> ToolResult:
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


def child_tool_definitions(owner: str, *, mode: str, task_tools: bool,
                           send_message=None, submit_plan=None) -> dict[str, ToolDefinition]:
    """Build the complete child capability contract before whitelist selection.

    Schemas, handlers and descriptions come from this single map so the prompt
    cannot advertise a tool that the child cannot execute.
    """
    if mode not in {"once", "persistent"}:
        raise ValueError(f"Unknown child mode: {mode}")
    definitions = shared_tool_definitions(owner, profile="child")
    if not task_tools:
        for name in TASK_TOOL_NAMES:
            definitions.pop(name)
    if mode == "persistent":
        definitions.update({
            "send_message": ToolDefinition(
                deepcopy(_CHILD_COORDINATION_SCHEMAS["send_message"]),
                send_message or (lambda **_: ToolResult(
                    "Error: child messaging is unavailable", "failed")),
                "child tool 'send_message'"),
            "submit_plan": ToolDefinition(
                deepcopy(_CHILD_COORDINATION_SCHEMAS["submit_plan"]),
                submit_plan or (lambda **_: ToolResult(
                    "Error: plan submission is unavailable", "failed")),
                "child tool 'submit_plan'"),
        })
    return definitions


def child_tool_prompt(definitions: dict[str, ToolDefinition], selected: tuple[str, ...]) -> str:
    """Render only the selected child tools and their descriptions."""
    if not selected:
        return "No tools are available; use only the supplied text."
    lines = [f"- {name}: {definitions[name].schema.get('description', '')}"
             for name in selected]
    return "Available child tools:\n" + "\n".join(lines)
