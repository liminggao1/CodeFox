"""子 agent 的工具、预算和恢复策略；完整委派通过共享 TurnEngine 执行。"""

from .. import core
from ..tools import shared_tools
from ..tools.tool_result import ToolResult
from ..runtime.tool_execution import ToolExecution, execute_tool
from ..runtime import hooks
from ..workspace import tasks
from ..context import compaction
from ..context.prompt_state import PromptState
from .turn_engine import TurnCallbacks, TurnEngine, TurnRequest
from ..providers import message_content
from ..providers.llm_client import APIConnectionError, StreamStopped
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
    publish_text = False
    preserve_tail = True

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
        self.automatic_compaction_enabled = True
        self.engine = None
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
        self.automatic_compaction_enabled = True
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

    def stopped(self):
        # Persistent work has its own shutdown protocol; Lead's turn stop only
        # interrupts synchronous once execution.
        return self.mode == "once" and interaction.stopped()

    def execute_tool(self, block) -> ToolExecution:
        """依次检查计划门禁和权限 hooks，再执行子 agent 工具。

        输入为原模型请求；拒绝不运行处理器或后置 Hook。
        返回真实工具结果和单独的后置错误，由回合先记录再结束执行。
        """
        if self.stopped():
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
                                     stopped=self.stopped,
                                     cwd=lambda: tasks.assignment_cwd(self.name))
        if check.status != "allowed":
            return ToolExecution(ToolResult(check.text, check.status))
        return execute_tool(block, self.handlers[block.name])

    def run(self, messages: list, *, before_iteration=None, before_complete=None):
        """执行完整委派，返回最终模型响应；不拥有信箱或 persistent 生命周期。

        模型、工具、历史追加和结果配对由共享 TurnEngine 管理。单次委派最多
        尝试一次上下文恢复；被截断的响应不会写入历史或产生工具执行效果。
        """
        # 获取当前用户请求文本，优先使用 active_request，为空则取第一条消息内容。
        request = self.active_request or (str(messages[0].get("content", "")) if messages else "")
        callbacks = TurnCallbacks(
            turn=self,
            assemble_tool_pool=lambda: (self.tools, self.handlers),
            build_context=self._build_context,
            system_factory=lambda: self.system,
            call_llm=self._request,
            execute_tool=lambda block, handlers: self.execute_tool(block),
            before_iteration=before_iteration,
            before_complete=before_complete,
        )
        context = interaction.current_context()
        self.engine = TurnEngine(callbacks)
        return self.engine.run(TurnRequest(
            messages, request,
            save_context=context.checkpoint if context is not None else None,
            prompt_state=self.prompt_state,
        ))

    def before_iteration(self, active_request):
        # 接收协调消息后、准备上下文前检查预算/调用次数限额。
        self.check_budget()
        return active_request

    def _build_context(self, messages, request):
        # 解析当前任务分配的工作目录；工具本身使用同一分配目录。
        tasks.assignment_cwd(self.name)
        assignment = tasks.assignments.get(self.name)
        # 默认上下文：无分配任务，工作目录为程序启动目录。
        context = "No assigned Task; workspace is the startup directory."
        if assignment:
            # 加载任务信息，获取任务工作目录。
            task = tasks.load_task(str(assignment["task_id"]))
            cwd = tasks.assignment_cwd(self.name)
            context = (f"Current Task: {task.id} [{task.status}] {task.subject}\n"
                       f"Task description: {task.description}\nWork directory: {cwd}")
        # 追加当前运行环境上下文，作为增量通知的字段，而不是重写 system。
        context += f"\n{self.work_context()}"
        values = {"current_delegation": request, "current_work": context}
        # 压缩摘要保留用户请求与当前任务上下文。
        return values, f"{request}\n\n[Current work]\n{context}"

    def after_preparation(self, result):
        if isinstance(result.error, (APIConnectionError, ConnectionError, TimeoutError,
                                     SubagentBudgetExceeded, StreamStopped)):
            raise result.error
        # 如果发生上下文压缩错误，打印日志，然后再次校验执行预算。
        if result.reason:
            core.terminal_print(f"[subagent compact] {result.reason}")
        self.check_budget()
        # 响应式恢复后的一次重建不重复摘要，后续正常轮次恢复自动压缩。
        self.automatic_compaction_enabled = True

    def recover_context(self, request):
        # 已执行过一次上下文恢复，保持原始模型错误，不再次请求摘要。
        if self._reactive_claimed:
            return False
        self._reactive_claimed = True
        # 摘要也使用计量客户端，预算耗尽不能多请求一次模型。
        self.check_budget()
        result = compaction.reactive_compact(
            self.history, request, client=self.client, model=self.model,
            strict_summary=True)
        if isinstance(result.error, (APIConnectionError, ConnectionError, TimeoutError,
                                     SubagentBudgetExceeded, StreamStopped)):
            raise result.error
        self.check_budget()
        # 压缩失败或未生效保留原历史，向生命周期管理层传播恢复失败。
        if result.reason or not result.applied:
            raise RuntimeError(f"Subagent context recovery failed: {result.reason}")
        # 成功后重置 prompt 状态并重新补齐完整上下文。
        self.prompt_state.reset()
        self.automatic_compaction_enabled = False
        self.engine.checkpoint()
        return True

    def accept_response(self, response, on_usage=None):
        # 模型因 max_tokens 截断输出，不追加响应或执行其中的工具。
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError("Subagent response was truncated (max_tokens)")
        text = message_content.extract_text(response.content)
        if text:
            self.last_text = text
        self._progress()
        return "accept"

    def after_tools(self, response, results, active_request, *, error=None):
        if not message_content.extract_text(response.content):
            # 本轮没有正文时，用最新工具批次替换旧证据，作为部分任务输出。
            self.last_text = "\n".join(str(item.get("content", "")) for item in results
                                       if item.get("content"))
        self._progress()
        # 致命 Hook 错误优先向上传播；预算终态在整批配对结果写回后处理。
        if error is None and self.tool_calls >= self.max_tool_calls and not self.stopped():
            raise SubagentBudgetExceeded("tool-call", self.max_tool_calls)

    def complete(self):
        return None

    def finish(self, status, reason="", *, response=None, error=None):
        # 生命周期管理层负责区分预算、网络和执行失败，保持原异常类型。
        if error is not None:
            raise error
        if status == "stopped":
            raise StreamStopped("Interrupted by user")
        if status != "completed":
            raise RuntimeError(reason or "Subagent execution failed")
        return response

    def check_budget(self):
        """在请求模型或等待审批前确认当前工作还有执行预算。"""
        if self.tool_calls >= self.max_tool_calls:
            raise SubagentBudgetExceeded("tool-call", self.max_tool_calls)
        if self.client.work_calls >= self.max_rounds:
            raise SubagentBudgetExceeded("model-call", self.max_rounds)

    def _request(self, prepared):
        # 请求使用脱离可变历史的快照，工具白名单与计量准备使用同一清单。
        call_id = uuid.uuid4().hex
        with interaction.operation_context(model_call_id=call_id):
            operation = interaction.operation_status("model", "请求模型", "running")
            try:
                response = self.client.messages.create(
                    model=self.model, system=prepared.system, messages=prepared.messages,
                    **({"tools": prepared.tools} if prepared.tools else {}), max_tokens=self.max_tokens)
            except Exception as exc:
                interaction.operation_status("model", "请求模型", "failed", operation_id=operation,
                                             reason=f"{type(exc).__name__}: {exc}")
                raise
            return response, call_id, operation
