"""Execute one teammate model round without owning its mailbox or lifecycle."""

from . import core
from . import file_tools
from . import hooks
from . import tasks
from pathlib import Path


class TeammateSession:
    def __init__(self, name, client, model, system, authorize_tool, send_message, submit_plan):
        """装配队友的模型参数、受限工具列表和执行回调。

        输入：name 为队友名称；client/model/system 为模型请求配置；
            authorize_tool 检查计划门禁，send_message 发信，submit_plan 提交审批。
        返回：None。保存配置并创建工具处理器；不请求模型、不启动线程。
        """
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
        """依次检查计划门禁和权限 hooks，再执行队友工具。

        输入：block 为包含 name 和 input 的模型工具请求。
        返回：拒绝原因、工具结果或执行错误的字符串。
            放行后调用处理器并触发 PostToolUse；拒绝时不执行处理器或后置 hook。
        """
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
        """按 core.DEFAULT_MAX_TOKENS 执行一次队友模型请求，并将响应和工具结果写回历史。

        输入：messages 为当前队友历史，会原地追加 assistant 和配对的 tool_result 消息。
        返回：模型响应对象；线程外层根据是否含工具请求决定继续工作或进入空闲。
        异常：响应因 max_tokens 截断时抛出 RuntimeError，不保存截断响应或执行其中的工具。
        """
        response = self.client.messages.create(
            model=self.model, system=self.system, messages=messages,
            tools=self.tools, max_tokens=core.DEFAULT_MAX_TOKENS)
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError("Teammate response was truncated (max_tokens)")
        messages.append({"role": "assistant", "content": response.content})
        calls = [block for block in response.content if block.type == "tool_use"]
        if calls:
            results = [{"type": "tool_result", "tool_use_id": block.id,
                        "content": self.execute_tool(block)} for block in calls]
            messages.append({"role": "user", "content": results})
        return response
