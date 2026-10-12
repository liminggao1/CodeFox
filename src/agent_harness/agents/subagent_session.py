"""Execute one subagent model round without owning its mailbox or lifecycle."""

from .. import core
from ..tools import shared_tools
from ..tools.tool_result import ToolResult
from ..runtime.tool_execution import ToolExecution, execute_tool
from ..runtime import hooks
from ..workspace import tasks
from ..context import compaction
from . import recovery
from ..providers import message_content
from ..runtime import interaction
import uuid


BASE_TOOL_NAMES = shared_tools.FILE_TOOL_NAMES
TASK_TOOL_NAMES = shared_tools.TASK_TOOL_NAMES
PERSISTENT_TOOL_NAMES = ("send_message", "submit_plan")


def selected_tool_names(tools, *, mode="once", task_tools=False):
    """Validate a whitelist against the capabilities of this child mode."""
    available = BASE_TOOL_NAMES
    if mode == "persistent":
        available += PERSISTENT_TOOL_NAMES
    if task_tools:
        available += TASK_TOOL_NAMES
    if tools is None:
        return available
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        raise ValueError("tools must be a list of tool names")
    unsupported = set(tools) - set(available)
    if unsupported:
        raise ValueError(f"Tools unavailable in this mode: {', '.join(sorted(unsupported))}")
    return tuple(t for t in available if t in tools)


class _MeteredClient:
    """Count execution and summary calls through the same injected client."""

    def __init__(self, client):
        self._client = client
        self.messages = self
        self.usage = {"tokens": 0, "model_calls": 0}

    def create(self, **kwargs):
        self.usage["model_calls"] += 1
        response = self._client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key, 0) if isinstance(usage, dict) else getattr(usage, key, 0)
            self.usage["tokens"] += int(value or 0)
        return response


class SubagentSession:
    def __init__(self, name, client, model, system, authorize_tool=None,
                 send_message=None, submit_plan=None, *, mode="persistent",
                 task_tools=True, work_context=None, tools=None, max_tokens=None):
        """Configure one child session without requesting a model or starting a thread.

        Input: child identity, client/model/system, mode, callbacks and tool whitelist.
            max_tokens controls execution output; summaries retain their own limit.
        Return: None. Builds the selected handlers and per-instance usage counter.
        """
        self.name = name
        self.client = _MeteredClient(client)
        self.usage = self.client.usage
        self.model = model
        self.max_tokens = core.DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        self.system = system
        self.authorize_tool = authorize_tool or (lambda block: None)
        self.work_context = work_context or (lambda: "")
        self.active_request = ""
        self._reactive_claimed = False
        shared = shared_tools.shared_tool_definitions(name, profile="child")

        self.tools = [
            shared["bash"].schema,
            shared["read_file"].schema,
            shared["write_file"].schema,
            shared["edit_file"].schema,
            shared["glob"].schema,
            {"name": "send_message",
             "description": "Send an intermediate message to 'lead' or an active subagent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            shared["list_tasks"].schema,
            shared["claim_task"].schema,
            shared["complete_task"].schema,
        ]

        self.handlers = {
            **{name: shared[name].handler for name in BASE_TOOL_NAMES},
            "send_message": lambda to, content: send_message(to, content),
            "submit_plan": lambda plan: submit_plan(plan),
            **{name: shared[name].handler for name in TASK_TOOL_NAMES},
        }

        selected = selected_tool_names(tools, mode=mode, task_tools=task_tools)
        self.tools = [tool for tool in self.tools if tool["name"] in selected]
        self.handlers = {key: value for key, value in self.handlers.items() if key in selected}
        self.system += (f"\nAvailable child tools: {', '.join(selected)}."
                        if selected else "\nNo tools are available; use only the supplied text.")

    def begin_work(self, prompt: str):
        """Set the current delegation and reset its single context recovery."""
        self.active_request = prompt
        self._reactive_claimed = False


    def execute_tool(self, block) -> ToolExecution:
        """依次检查计划门禁和权限 hooks，再执行子 agent 工具。

        输入为原模型请求；拒绝不运行处理器或后置 Hook。
        返回真实工具结果和单独的后置错误，由回合先记录再结束执行。
        """
        if block.name not in self.handlers:
            return ToolExecution(ToolResult(f"Error: Tool '{block.name}' is unavailable for this child", "failed"))
        check = hooks.check_tool_use(block, authorize=self.authorize_tool,
                                     cwd=lambda: tasks.assignment_cwd(self.name))
        if check.status != "allowed":
            return ToolExecution(ToolResult(check.text, check.status))
        return execute_tool(block, self.handlers[block.name])

    def run(self, messages: list):
        """Request one response and append its paired tool results.

        Input: the mutable conversation for the current delegation.
        Return: a complete model response. Context recovery is attempted at most
            once per delegation; truncated responses produce no tool effects.
        """
        request = self.active_request or str(messages[0].get("content", ""))
        tasks.assignment_cwd(self.name)
        assignment = tasks.assignments.get(self.name)
        context = "No assigned Task; workspace is the startup directory."
        if assignment:
            task = tasks.load_task(str(assignment["task_id"]))
            cwd = tasks.assignment_cwd(self.name)
            context = (f"Current Task: {task.id} [{task.status}] {task.subject}\n"
                       f"Task description: {task.description}\nWork directory: {cwd}")
        context += f"\n{self.work_context()}"
        request = f"{request}\n\n[Current work]\n{context}"
        result = compaction.prepare_history(
            messages, request, client=self.client, model=self.model,
            system=self.system, tools=self.tools, preserve_tail=True)
        if result.reason:
            core.terminal_print(f"[subagent compact] {result.reason}")
        try:
            response = self._request(messages, request)
        except Exception as exc:
            if not recovery.is_prompt_too_long_error(exc) or self._reactive_claimed:
                raise
            self._reactive_claimed = True
            result = compaction.reactive_compact(
                messages, request, client=self.client, model=self.model,
                strict_summary=True)
            if result.reason or not result.applied:
                raise RuntimeError(f"Subagent context recovery failed: {result.reason}") from exc
            response = self._request(messages, request)
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError("Subagent response was truncated (max_tokens)")
        messages.append({"role": "assistant", "content": response.content})
        calls = [block for block in response.content if block.type == "tool_use"]
        if calls:
            results = []
            with interaction.operation_context(model_call_id=self._model_call_id):
                for block in calls:
                    interaction.publish("tool_call", id=block.id, name=block.name, input=block.input)
                context = interaction.current_context()
                if context is not None and context.checkpoint is not None:
                    context.checkpoint()
                for index, block in enumerate(calls):
                    execution = self.execute_tool(block)
                    result = execution.result.model_result(block.id)
                    results.append(result)
                    interaction.publish("tool_result", **result, status=execution.result.status)
                    if execution.post_error is not None:
                        # 后置失败不执行后续工具，但必须补齐这批调用的配对结果。
                        for pending in calls[index + 1:]:
                            stopped = ToolResult("Interrupted before execution", "stopped").model_result(pending.id)
                            results.append(stopped)
                            interaction.publish("tool_result", **stopped, status="stopped")
                        messages.append({"role": "user", "content": results})
                    if context is not None and context.checkpoint is not None:
                        context.checkpoint()
                    if execution.post_error is not None:
                        raise execution.post_error
            messages.append({"role": "user", "content": results})
        return response

    def _request(self, messages, request):
        self._model_call_id = uuid.uuid4().hex
        with interaction.operation_context(model_call_id=self._model_call_id):
            operation = interaction.operation_status("model", "请求模型", "running")
            try:
                response = self.client.messages.create(
                    model=self.model, system=(f"{self.system}\n\n[Current delegation]\n{request}\n\n"
                                             "Earlier delegations in history are reference only; follow the current delegation."),
                    messages=messages,
                    **({"tools": self.tools} if self.tools else {}), max_tokens=self.max_tokens)
            except Exception as exc:
                interaction.operation_status("model", "请求模型", "failed", operation_id=operation,
                                             reason=f"{type(exc).__name__}: {exc}")
                raise
            interaction.operation_status("model", "请求模型", "truncated" if getattr(response, "stop_reason", None) == "max_tokens" else "completed",
                                         operation_id=operation)
            return response
