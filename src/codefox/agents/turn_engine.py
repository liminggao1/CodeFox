"""Lead 与子 agent 共用的工作回合执行机。

本模块拥有模型—工具循环的执行顺序、请求快照、历史写入和终态，不负责
会话持久化、前端渲染或运行时线程生命周期。调用方通过 TurnCallbacks 提供
装配入口，由现有的 LeadTurn / SubagentSession 持有各自的执行与收尾策略。
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..context.prompt_state import PromptState
from ..providers import message_content
from ..providers.llm_client import StreamStopped, model_messages
from ..runtime import interaction
from ..runtime.session_store import json_value
from ..runtime.tool_execution import ToolExecution
from ..tools.tool_result import ToolResult
from . import recovery


class TurnPhase(str, Enum):
    """一项工作回合的内部生命周期阶段。"""

    CREATED = "created"
    PREPARING = "preparing"
    WAITING_MODEL = "waiting_model"
    PROCESSING_RESPONSE = "processing_response"
    EXECUTING_TOOLS = "executing_tools"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class TurnRequest:
    """由调用方提供的一项完整工作输入。"""

    history: list
    active_request: str
    save_context: Callable[[], None] | None = None
    on_usage: Callable[[object], None] | None = None
    prompt_state: PromptState | None = None


@dataclass(frozen=True)
class PreparedRequest:
    """API request content detached from the mutable session history."""

    system: str
    messages: list[dict]
    tools: list[dict]

    @classmethod
    def create(cls, system: str, history: list, tools: list[dict]):
        return cls(system, copy.deepcopy(model_messages(history)), copy.deepcopy(tools))


@dataclass(frozen=True)
class TurnCallbacks:
    """工作回合的装配入口，不把每一步执行拆成回调。

    turn 为现有的 LeadTurn 或 SubagentSession，提供客户端、模型、停止判断和
    迭代、响应、恢复、工具后处理、收尾策略。引擎独占请求快照、历史追加、
    事件发布和工具批次配对；execute_tool 只结算一个 ToolExecution。
    """

    turn: object
    assemble_tool_pool: Callable[[], tuple[list[dict], dict]]
    build_context: Callable[[list, str], tuple[dict, str]]
    system_factory: Callable[[], str]
    call_llm: Callable[[PreparedRequest], tuple]
    execute_tool: Callable[[object, dict], ToolExecution]
    build_user_content: Callable[[list[dict]], list[dict]] = list
    before_iteration: Callable[[], None] | None = None
    before_complete: Callable[[], bool] | None = None


class TurnEngine:
    """执行一项完整工作，共用执行顺序并隔离每个调用方的状态。"""

    _ALLOWED_PHASES = {
        TurnPhase.CREATED: {TurnPhase.PREPARING, TurnPhase.STOPPED, TurnPhase.FAILED},
        TurnPhase.PREPARING: {
            TurnPhase.WAITING_MODEL, TurnPhase.WAITING_APPROVAL,
            TurnPhase.STOPPED, TurnPhase.FAILED,
        },
        TurnPhase.WAITING_MODEL: {
            TurnPhase.PREPARING, TurnPhase.PROCESSING_RESPONSE,
            TurnPhase.STOPPED, TurnPhase.FAILED,
        },
        TurnPhase.PROCESSING_RESPONSE: {
            TurnPhase.PREPARING, TurnPhase.EXECUTING_TOOLS,
            TurnPhase.COMPLETED, TurnPhase.STOPPED, TurnPhase.FAILED,
        },
        TurnPhase.EXECUTING_TOOLS: {
            TurnPhase.PREPARING, TurnPhase.WAITING_APPROVAL,
            TurnPhase.COMPLETED, TurnPhase.STOPPED, TurnPhase.FAILED,
        },
        TurnPhase.WAITING_APPROVAL: {
            TurnPhase.PREPARING, TurnPhase.EXECUTING_TOOLS,
            TurnPhase.STOPPED, TurnPhase.FAILED,
        },
        TurnPhase.COMPLETED: set(),
        TurnPhase.STOPPED: set(),
        TurnPhase.FAILED: set(),
    }

    def __init__(self, callbacks: TurnCallbacks):
        self.callbacks = callbacks
        self.phase = TurnPhase.CREATED
        self._phase_history = (TurnPhase.CREATED,)
        self.model_call_id = None
        self._text_indices = {}

    @property
    def phase_history(self) -> tuple[TurnPhase, ...]:
        """返回本轮已经经过的阶段，不暴露可变内部列表。"""
        return self._phase_history

    def _set_phase(self, phase: TurnPhase) -> None:
        if phase == self.phase:
            return
        if phase not in self._ALLOWED_PHASES[self.phase]:
            raise RuntimeError(f"非法 TurnPhase 转换: {self.phase.value} -> {phase.value}")
        self.phase = phase
        self._phase_history = (*self._phase_history, phase)

    def _observe_phase(self, value: str) -> None:
        """接收运行时嵌套操作发出的阶段变化。"""
        self._set_phase(TurnPhase(value))

    @contextmanager
    def waiting_approval(self):
        """轮次间计划等待结束后回到原阶段，不消耗执行预算。"""
        previous = self.phase
        self._set_phase(TurnPhase.WAITING_APPROVAL)
        try:
            yield
        finally:
            self._set_phase(previous)

    def _finish(self, status: str, reason: str = "", *, response=None, error=None):
        if status == "stopped":
            self._set_phase(TurnPhase.STOPPED)
        elif status == "failed":
            self._set_phase(TurnPhase.FAILED)
        else:
            self._set_phase(TurnPhase.COMPLETED)
        return self.callbacks.turn.finish(status, reason, response=response, error=error)

    def checkpoint(self):
        if self._request.save_context is not None:
            self._request.save_context()

    def append(self, message):
        """追加模型历史，发布已接受的助手消息块，再触发保存检查点。"""
        self._request.history.append(message)
        if message.get("role") == "assistant":
            identity = self._model_identity()
            for index, block in enumerate(json_value(message["content"])):
                if block.get("type") == "text" and self.callbacks.turn.publish_text:
                    interaction.publish("assistant_message", text=block["text"],
                                        block_index=self._text_indices.get(index, index), **identity)
                elif block.get("type") == "tool_use":
                    interaction.publish("tool_call", **block, **identity)
        self.checkpoint()

    def _model_identity(self):
        return {"model_call_id": self.model_call_id} if self.model_call_id else {}

    def _record_tool_result(self, result, status):
        """整批回填前逐项发布、保存真实结果，回填时不重复发布。

        进程在工具之间中断，恢复时可以复用已结算项，仅为缺失项补充中断结果。
        """
        interaction.publish("tool_result", **json_value(result), status=status,
                            **self._model_identity())
        self.checkpoint()

    def _execute_tools(self, content, handlers):
        """按模型顺序逐项结算；异常也补齐整批调用的配对结果。"""
        # 非工具块保留在助手消息中，无需执行。
        calls = [block for block in content if block.type == "tool_use"]
        results = []
        with interaction.operation_context(model_call_id=self.model_call_id):
            for index, block in enumerate(calls):
                try:
                    execution = self.callbacks.execute_tool(block, handlers)
                except Exception as exc:
                    # 前置 Hook 或目录解析异常，也必须补齐模型已提交的整批调用。
                    execution = ToolExecution(ToolResult(
                        f"Error: {type(exc).__name__}: {exc}", "failed"), exc)
                # 将同步工具输出与原调用 ID 配对，再逐项发布和保存检查点。
                result = execution.result.model_result(block.id)
                results.append(result)
                self._record_tool_result(result, execution.result.status)
                if execution.post_error is not None:
                    # 保留真实副作用；剩余工具不运行，但每个调用都获得 stopped 结果。
                    for pending in calls[index + 1:]:
                        stopped = ToolResult("Interrupted before execution", "stopped").model_result(pending.id)
                        results.append(stopped)
                        self._record_tool_result(stopped, "stopped")
                    return results, execution.post_error
        return results, None

    def run(self, request: TurnRequest):
        """执行工作直到完成、失败或停止，由调用方转换返回值或传播异常。"""
        self._request = request
        self.phase = TurnPhase.CREATED
        self._phase_history = (TurnPhase.CREATED,)
        self.model_call_id = None
        self._text_indices = {}
        # 嵌套 once 子 agent 使用自己的观察器，离开时恢复父回合的观察器。
        with interaction.operation_context(phase_observer=self._observe_phase):
            try:
                return self._run(request)
            except Exception:
                if self.phase not in {TurnPhase.COMPLETED, TurnPhase.STOPPED, TurnPhase.FAILED}:
                    self._set_phase(TurnPhase.FAILED)
                raise

    def _run(self, request: TurnRequest):
        # 调用方持有自己的配置、预算和恢复状态；历史只有当前请求这一句柄。
        turn = self.callbacks.turn
        turn.history = request.history
        turn.engine = self
        prompt_state = request.prompt_state if request.prompt_state is not None else PromptState()
        turn.prompt_state = prompt_state
        prepared_request: PreparedRequest | None = None
        handlers = {}
        active_request = request.active_request
        summary_request = active_request

        # -- 准备上下文并请求完整模型响应 --
        # 每次循环迭代：调用一次模型，处理文本回答或工具调用请求。
        while True:
            # 外部停止信号由各自策略判断，persistent 不继承 Lead 的单轮停止。
            if turn.stopped():
                return self._finish("stopped")

            self._set_phase(TurnPhase.PREPARING)
            # 准备阶段错误不消耗模型上下文恢复机会。
            model_call_started = False
            try:
                if prepared_request is None:
                    if self.callbacks.before_iteration is not None:
                        self.callbacks.before_iteration()
                    active_request = turn.before_iteration(active_request)
                    # 一次准备固定 system、动态通知与工具快照，计量和发送复用实际请求。
                    values, summary_request = self.callbacks.build_context(turn.history, active_request)
                    tools, handlers = self.callbacks.assemble_tool_pool()
                    previous_prompt = prompt_state.snapshot()
                    # 按上下文预算裁剪或压缩历史消息，包含将要追加的运行上下文。
                    system, compacted = prompt_state.prepare(
                        turn.history, values, self.callbacks.system_factory, summary_request,
                        enabled=turn.automatic_compaction_enabled,
                        client=turn.client, model=turn.model, tools=tools,
                        preserve_tail=turn.preserve_tail,
                    )
                    prepared_request = PreparedRequest.create(system, turn.history, tools)
                    # 快照、通知和压缩后的历史在同一检查点保存，重试不重复追加。
                    if compacted.applied or prompt_state.snapshot() != previous_prompt:
                        self.checkpoint()
                    turn.after_preparation(compacted)
                # 重试复用同一份 API 请求内容，不重新读取可变历史。
                if turn.stopped():
                    return self._finish("stopped")

                self._set_phase(TurnPhase.WAITING_MODEL)
                model_call_started = True
                # 清除上次调用关联，避免新请求失败时沿用上次已接受的调用 ID。
                self.model_call_id = None
                self._text_indices = {}
                # 重试策略和模型降级由调用方的请求入口处理。
                response, call_id, operation_id = self.callbacks.call_llm(prepared_request)
            except Exception as exc:
                if isinstance(exc, StreamStopped):
                    return self._finish("stopped", error=exc)
                # 首次遇到模型上下文过长错误时，按调用方策略恢复，再重新准备快照。
                if not turn.stopped() and model_call_started and recovery.is_prompt_too_long_error(exc):
                    try:
                        if turn.recover_context(summary_request):
                            prepared_request = None
                            continue
                    except Exception as recovery_error:
                        exc = recovery_error
                return self._finish("failed", f"{type(exc).__name__}: {exc}", error=exc)

            # -- 接受工具请求前，先处理输出截断的重试与续写 --
            self._set_phase(TurnPhase.PROCESSING_RESPONSE)
            self.model_call_id = call_id
            self._text_indices = getattr(response, "stream_indices", {})
            try:
                action = turn.accept_response(response, request.on_usage)
            except Exception:
                if call_id:
                    interaction.operation_status(
                        "model", "请求模型",
                        "truncated" if getattr(response, "stop_reason", None) == "max_tokens" else "failed",
                        operation_id=operation_id, model_call_id=call_id)
                raise
            if call_id:
                status = ("discarded" if action == "retry" else
                          "truncated" if action in {"continue", "stop"} else "completed")
                interaction.operation_status("model", "请求模型", status,
                                             operation_id=operation_id, model_call_id=call_id)

            if turn.stopped() and action in {"retry", "continue", "stop"}:
                # 截断的工具请求不能执行或保存，只保留可用文本。
                text = message_content.extract_text(response.content)
                if text:
                    self.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
                return self._finish("stopped", response=response)
            if action == "retry":
                continue

            prepared_request = None
            if action in {"continue", "stop"}:
                # 输出额度升级的请求返回后，才保留可续写的部分文本。
                self.append({"role": "assistant", "content": response.content})
                if action == "continue":
                    # 添加续写提示，作为下一次模型调用的用户消息。
                    self.append({"role": "user", "content": turn.continuation_prompt})
                    continue
                # 续写机会耗尽时按调用方策略收尾。
                return self._finish("truncated", "Output continuation limit reached", response=response)

            # 保存模型响应，供工具结果和后续调用引用。
            self.append({"role": "assistant", "content": response.content})
            # -- 由调用方决定当前工作是否允许结束 --
            if not message_content.has_tool_use(response.content):
                # 模型输出不含工具调用
                if turn.stopped():
                    # turn标记为已停止（外部触发终止：超时、手动停止、任务中止）
                    return self._finish("stopped", response=response)
                if (self.callbacks.before_complete is not None
                        and self.callbacks.before_complete()):
                    # 存在完成前回调钩子，并且钩子返回True：不结束，继续循环
                    continue
                feedback = turn.complete()
                # 执行turn的complete逻辑，标记本轮任务尝试完成，生成反馈信息
                if turn.stopped():
                    # complete过程中turn被置为停止，中止任务
                    return self._finish("stopped", response=response)
                if feedback is not None:
                    # complete返回了反馈文本：把反馈作为user消息追加进对话，继续循环
                    self.append({"role": "user", "content": feedback})
                    continue
                # 没有停止、没有额外反馈，任务正常结束
                return self._finish("completed", response=response)
            # -- 逐项结算工具，再将完整结果批次交给模型 --
            # 当前阶段由 TurnEngine 管理，单工具入口只负责执行和结算。
            self._set_phase(TurnPhase.EXECUTING_TOOLS)
            results, post_error = self._execute_tools(response.content, handlers)
            # 通知装配失败也保留已经执行的整批结果，不留下未配对的工具调用。
            content = results
            if post_error is None:
                try:
                    content = self.callbacks.build_user_content(results)
                except Exception as exc:
                    post_error = exc
            # 致命错误先把整批真实/停止结果写回历史，再向上传播。
            self.append({"role": "user", "content": content})
            turn.after_tools(response, results, active_request, error=post_error)
            if post_error is not None:
                raise post_error
            if turn.stopped():
                return self._finish("stopped", response=response)
