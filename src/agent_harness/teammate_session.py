"""Execute one teammate model round without owning its mailbox or lifecycle."""

from . import file_tools
from . import hooks
from . import tasks
from pathlib import Path


class TeammateSession:
    def __init__(self, name, client, model, system, authorize_tool, send_message, submit_plan):
        self.name = name
        self.client = client
        self.model = model
        self.system = system
        self.authorize_tool = authorize_tool
        def current_cwd() -> tuple[Path | None, str | None]:
            if name not in tasks.teammate_assignments:
                return None, "Error: Claim a Task before using workspace tools."
            try:
                return tasks.assignment_cwd(name), None
            except (FileNotFoundError, ValueError) as exc:
                return None, f"Error: Invalid task assignment: {exc}"

        def _run_bash(command: str) -> str:
            cwd, error = current_cwd()
            return error or file_tools.run_bash(command, cwd=cwd)

        def _run_read(path: str, limit: int | None = None,
                      offset: int = 0) -> str:
            cwd, error = current_cwd()
            return error or file_tools.run_read(path, limit=limit, offset=offset, cwd=cwd)

        def _run_write(path: str, content: str) -> str:
            cwd, error = current_cwd()
            return error or file_tools.run_write(path, content, cwd=cwd)

        def _run_edit(path: str, old_text: str, new_text: str) -> str:
            cwd, error = current_cwd()
            return error or file_tools.run_edit(path, old_text, new_text, cwd=cwd)

        def _run_glob(pattern: str) -> str:
            cwd, error = current_cwd()
            return error or file_tools.run_glob(pattern, cwd=cwd)

        def _run_list_tasks():
            task_list = tasks.list_tasks()
            if not task_list:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in task_list)

        def _run_claim_task(task_id: str):
            try:
                return tasks.claim_task(task_id, owner=name)
            except ValueError as exc:
                return f"Error: {exc}"
            except FileNotFoundError:
                return f"Error: Task {task_id} not found"

        def _run_complete_task(task_id: str):
            try:
                return tasks.complete_task(task_id, owner=name)
            except ValueError as exc:
                return f"Error: {exc}"
            except FileNotFoundError:
                return f"Error: Task {task_id} not found"

        self.tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "path": {"type": "string"},
                                  "limit": {"type": "integer"},
                                  "offset": {"type": "integer"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace text in a file.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "path": {"type": "string"},
                                  "old_text": {"type": "string"},
                                  "new_text": {"type": "string"}},
                              "required": ["path", "old_text", "new_text"]}},
            {"name": "glob", "description": "Find files by glob pattern; ** matches recursively.",
             "input_schema": {"type": "object",
                              "properties": {
                                  "pattern": {"type": "string"}},
                              "required": ["pattern"]}},
            {"name": "send_message",
             "description": "Send an intermediate message to 'lead' or an active teammate.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks on the board.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        self.handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write, "edit_file": _run_edit,
            "glob": _run_glob,
            "send_message": lambda to, content: send_message(to, content),
            "submit_plan": lambda plan: submit_plan(plan),
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }


    def execute_tool(self, block) -> str:
        """Apply the current assignment gate and hooks before calling a handler."""
        blocked = self.authorize_tool(block)
        if blocked is not None:
            return str(blocked)
        blocked = hooks.trigger_hooks("PreToolUse", block)
        if blocked is not None:
            return str(blocked)
        output = file_tools.call_tool_handler(self.handlers.get(block.name), block.input, block.name)
        hooks.trigger_hooks("PostToolUse", block, output)
        return str(output)

    def run(self, messages: list):
        """Append a model response and a complete batch of tool results; return it."""
        response = self.client.messages.create(
            model=self.model, system=self.system, messages=messages,
            tools=self.tools, max_tokens=8000)
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError("Teammate response was truncated (max_tokens)")
        messages.append({"role": "assistant", "content": response.content})
        calls = [block for block in response.content if block.type == "tool_use"]
        if calls:
            results = [{"type": "tool_result", "tool_use_id": block.id,
                        "content": self.execute_tool(block)} for block in calls]
            messages.append({"role": "user", "content": results})
        return response
