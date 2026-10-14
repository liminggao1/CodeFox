"""Execute one subagent model round without owning its mailbox or lifecycle."""

from .. import core
from ..tools import shared_tools
from ..tools.tool_result import ToolResult
from ..runtime.tool_execution import ToolExecution, execute_tool
from ..runtime import hooks
from ..workspace import tasks
from ..context import compaction
from ..context.prompt_state import PromptState
from . import recovery
from ..providers import message_content
from ..providers.llm_client import APIConnectionError, model_messages
from ..runtime import interaction
import uuid


def selected_tool_names(tools, *, mode="once", task_tools=False, definitions=None):
    """Validate a whitelist against the capabilities of this child mode."""
    if definitions is None:
        definitions = shared_tools.child_tool_definitions("child", mode=mode, task_tools=task_tools)
    available = tuple(definitions)
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

    def __init__(self, client, max_calls=None, progress=None):
        self._client = client
        self.messages = self
        self.max_calls = max_calls
        self.progress = progress
        self.usage = {"tokens": 0, "model_calls": 0,
                      "input_tokens": 0, "output_tokens": 0}
        self.work_calls = 0

    def create(self, **kwargs):
        if self.max_calls is not None and self.work_calls >= self.max_calls:
            raise SubagentBudgetExceeded("model-call", self.max_calls)
        self.work_calls += 1
        self.usage["model_calls"] += 1
        if self.progress is not None:
            self.progress()
        try:
            response = self._client.messages.create(**kwargs)
        except (APIConnectionError, ConnectionError, TimeoutError):
            # A refused/unreachable endpoint is an external connectivity failure,
            # not a consumed child execution attempt.
            self.work_calls -= 1
            self.usage["model_calls"] -= 1
            if self.progress is not None:
                self.progress()
            raise
        except Exception:
            if self.progress is not None:
                self.progress()
            raise
        usage = getattr(response, "usage", None)
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key, 0) if isinstance(usage, dict) else getattr(usage, key, 0)
            self.usage[key] += int(value or 0)
        self.usage["tokens"] = self.usage["input_tokens"] + self.usage["output_tokens"]
        if self.progress is not None:
            self.progress()
        return response


class SubagentBudgetExceeded(RuntimeError):
    """Stop a child work unit when a model or tool budget is exhausted."""

    def __init__(self, kind: str, limit: int):
        self.kind = kind
        self.limit = limit
        super().__init__(f"Subagent {kind} budget exhausted ({limit})")


class SubagentSession:
    def __init__(self, name, client, model, system, authorize_tool=None,
                 send_message=None, submit_plan=None, *, mode="persistent",
                 task_tools=True, work_context=None, tools=None, max_tokens=None,
                 max_rounds=None, max_tool_calls=None, progress=None):
        """Configure one child session without requesting a model or starting a thread.

        Input: child identity, client/model/system, mode, callbacks and tool whitelist.
            max_tokens controls execution output; summaries retain their own limit.
        Return: None. Builds the selected handlers and per-instance usage counter.
        """
        self.name = name
        self.mode = mode
        self.max_rounds = core.SUBAGENT_MAX_ROUNDS if max_rounds is None else max_rounds
        self.max_tool_calls = (core.SUBAGENT_MAX_TOOL_CALLS
                               if max_tool_calls is None else max_tool_calls)
        if type(self.max_rounds) is not int or self.max_rounds <= 0:
            raise ValueError("max_rounds must be a positive integer")
        if type(self.max_tool_calls) is not int or self.max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be a positive integer")
        self.rounds = 0
        self.tool_calls = 0
        self.progress = progress
        self.client = _MeteredClient(client, max_calls=self.max_rounds,
                                     progress=self._client_progress)
        self.usage = self.client.usage
        self.model = model
        self.max_tokens = core.DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        self.system = system
        self.authorize_tool = authorize_tool or (lambda block: None)
        self.work_context = work_context or (lambda: "")
        self.active_request = ""
        self.last_text = ""
        self._reactive_claimed = False
        self.prompt_state = PromptState()
        send_handler = (lambda to, content: send_message(to, content)
                        if send_message is not None else ToolResult(
                            "Error: child messaging is unavailable", "failed"))
        plan_handler = (lambda plan: submit_plan(plan)
                        if submit_plan is not None else ToolResult(
                            "Error: plan submission is unavailable", "failed"))
        definitions = shared_tools.child_tool_definitions(
            name, mode=mode, task_tools=task_tools,
            send_message=send_handler,
            submit_plan=plan_handler,
        )
        selected = selected_tool_names(tools, mode=mode, task_tools=task_tools,
                                       definitions=definitions)
        self.tools = [definitions[item].schema for item in selected]
        self.handlers = {item: definitions[item].handler for item in selected}
        self.system += "\n" + shared_tools.child_tool_prompt(definitions, selected)
        self.system += ("\nFollow the latest Current delegation in runtime context; "
                        "earlier delegations in history are reference only.")
        self.tools.sort(key=lambda tool: tool["name"])

    def begin_work(self, prompt: str):
        """Set the current delegation and reset its single context recovery."""
        self.active_request = prompt
        self._reactive_claimed = False
        self.rounds = 0
        self.tool_calls = 0
        self.client.work_calls = 0
        self.last_text = ""
        self._progress()

    def _progress(self):
        if self.progress is not None:
            self.progress(self)

    def _client_progress(self):
        self.rounds = self.client.work_calls
        self._progress()

    def _stopped(self):
        # Persistent work has its own shutdown protocol; Lead's turn stop only
        # interrupts synchronous once execution.
        return self.mode == "once" and interaction.stopped()

    def execute_tool(self, block) -> ToolExecution:
        """依次检查计划门禁和权限 hooks，再执行子 agent 工具。

        输入为原模型请求；拒绝不运行处理器或后置 Hook。
        返回真实工具结果和单独的后置错误，由回合先记录再结束执行。
        """
        if self._stopped():
            return ToolExecution(ToolResult("Interrupted before execution", "stopped"))
        if self.tool_calls >= self.max_tool_calls:
            return ToolExecution(ToolResult(
                f"Stopped: subagent tool-call budget exhausted ({self.max_tool_calls})",
                "stopped"))
        self.tool_calls += 1
        self._progress()
        if block.name not in self.handlers:
            return ToolExecution(ToolResult(f"Error: Tool '{block.name}' is unavailable for this child", "failed"))
        if (block.name == "read_file" and isinstance(block.input, dict)
                and shared_tools.file_tools.is_session_tool_result_archive(
                    block.input.get("path", ""), tasks.assignment_cwd(self.name))):
            return ToolExecution(ToolResult(
                "Error: session tool-result archives are not readable through the generic child read_file tool; "
                "read the original project file with offset/limit. Lead can query task results.", "failed"))
        check = hooks.check_tool_use(block, authorize=self.authorize_tool,
                                     stopped=self._stopped,
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
        self.check_budget()
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
        values = {"current_delegation": request, "current_work": context}
        request = f"{request}\n\n[Current work]\n{context}"
        system, result = self.prompt_state.prepare(
            messages, values, lambda: self.system, request, client=self.client,
            model=self.model, tools=self.tools, preserve_tail=True)
        if result.reason:
            core.terminal_print(f"[subagent compact] {result.reason}")
        self.check_budget()
        try:
            response = self._request(messages, system)
        except Exception as exc:
            if not recovery.is_prompt_too_long_error(exc) or self._reactive_claimed:
                raise
            self._reactive_claimed = True
            result = compaction.reactive_compact(
                messages, request, client=self.client, model=self.model,
                strict_summary=True)
            self.check_budget()
            if result.reason or not result.applied:
                raise RuntimeError(f"Subagent context recovery failed: {result.reason}") from exc
            self.prompt_state.reset()
            system, _ = self.prompt_state.prepare(
                messages, values, lambda: self.system, request, client=self.client,
                model=self.model, tools=self.tools, enabled=False, preserve_tail=True)
            response = self._request(messages, system)
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError("Subagent response was truncated (max_tokens)")
        text = message_content.extract_text(response.content)
        if text:
            self.last_text = text
        self._progress()
        messages.append({"role": "assistant", "content": response.content})
        calls = [block for block in response.content if block.type == "tool_use"]
        if calls:
            results = []
            post_error = None
            with interaction.operation_context(model_call_id=self._model_call_id):
                for block in calls:
                    interaction.publish("tool_call", id=block.id, name=block.name, input=block.input)
                context = interaction.current_context()
                if context is not None and context.checkpoint is not None:
                    context.checkpoint()
                for index, block in enumerate(calls):
                    try:
                        execution = self.execute_tool(block)
                    except Exception as exc:
                        # 前置 Hook 或目录解析异常也必须补齐模型已提交的整批调用。
                        execution = ToolExecution(ToolResult(
                            f"Error: {type(exc).__name__}: {exc}", "failed"), exc)
                    result = execution.result.model_result(block.id)
                    results.append(result)
                    interaction.publish("tool_result", **result, status=execution.result.status)
                    if execution.post_error is not None:
                        # 执行失败不运行后续工具，但必须补齐这批调用的配对结果。
                        for pending in calls[index + 1:]:
                            stopped = ToolResult("Interrupted before execution", "stopped").model_result(pending.id)
                            results.append(stopped)
                            interaction.publish("tool_result", **stopped, status="stopped")
                        post_error = execution.post_error
                    if context is not None and context.checkpoint is not None:
                        context.checkpoint()
                    if execution.post_error is not None:
                        break
            messages.append({"role": "user", "content": results})
            if not text:
                # 本轮没有模型正文时，用最新工具批次替换旧证据。
                self.last_text = "\n".join(
                    str(item.get("content", "")) for item in results
                    if item.get("content")
                )
            self._progress()
            if post_error is not None:
                raise post_error
            if self.tool_calls >= self.max_tool_calls and not self._stopped():
                raise SubagentBudgetExceeded("tool-call", self.max_tool_calls)
        return response

    def check_budget(self):
        """在请求模型或等待审批前确认当前工作还有执行预算。"""
        if self.tool_calls >= self.max_tool_calls:
            raise SubagentBudgetExceeded("tool-call", self.max_tool_calls)
        if self.client.work_calls >= self.max_rounds:
            raise SubagentBudgetExceeded("model-call", self.max_rounds)

    def _request(self, messages, system):
        self._model_call_id = uuid.uuid4().hex
        with interaction.operation_context(model_call_id=self._model_call_id):
            operation = interaction.operation_status("model", "请求模型", "running")
            try:
                response = self.client.messages.create(
                    model=self.model, system=system,
                    messages=model_messages(messages),
                    **({"tools": self.tools} if self.tools else {}), max_tokens=self.max_tokens)
            except Exception as exc:
                interaction.operation_status("model", "请求模型", "failed", operation_id=operation,
                                             reason=f"{type(exc).__name__}: {exc}")
                raise
            interaction.operation_status("model", "请求模型", "truncated" if getattr(response, "stop_reason", None) == "max_tokens" else "completed",
                                         operation_id=operation)
            return response
