"""Lead Turn 执行机。

本模块拥有一轮模型-工具循环的执行顺序和终态，不负责会话持久化、前端渲染或
运行时线程生命周期。``host`` 通过 ``TurnCallbacks`` 注入现有装配逻辑，保持
旧的 ``host.agent_loop`` 入口和测试替身可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .. import core
from ..context import compaction, prompts
from ..context.prompt_state import PromptState
from ..providers import message_content
from ..providers.llm_client import StreamStopped
from ..runtime import hooks, interaction
from . import recovery
from .lead_tools import execute_tool_batch
from .lead_turn import LeadTurn


class TurnPhase(str, Enum):
    """一轮 Lead 回合的内部生命周期阶段。"""

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
    """由调用方提供的一轮模型-工具回合输入。"""

    history: list
    active_request: str
    save_context: Callable[[], None] | None = None
    on_usage: Callable[[object], None] | None = None
    prompt_state: PromptState | None = None


@dataclass(frozen=True)
class TurnCallbacks:
    """由 host 持有的适配回调，执行机不直接导入 ``host``。"""

    assemble_tool_pool: Callable[[], tuple[list[dict], dict]]
    build_context: Callable[[list], object]
    call_llm: Callable[[list, str, list, recovery.RecoveryState], tuple]
    build_user_content: Callable[[list[dict]], list[dict]]
    evaluate_goal: Callable[[list], object | None]
    remember_after_turn: Callable[[list], None]
    goal_condition: Callable[[], str]


class TurnEngine:
    """在小而可测试的接口后执行一轮 Lead 回合。"""

    def __init__(self, callbacks: TurnCallbacks):
        self.callbacks = callbacks
        self.phase = TurnPhase.CREATED

    def _set_phase(self, phase: TurnPhase) -> None:
        self.phase = phase

    def _finish(self, turn: LeadTurn, status: str, reason: str = ""):
        if status == "stopped":
            self._set_phase(TurnPhase.STOPPED)
        elif status == "failed":
            self._set_phase(TurnPhase.FAILED)
        else:
            self._set_phase(TurnPhase.COMPLETED)
        return turn.finish(status, reason)

    def run(self, request: TurnRequest):
        """执行一轮回合，直到模型完成、失败或收到停止请求。"""
        self._set_phase(TurnPhase.CREATED)
        # 创建本轮回合对象 LeadTurn：管理本轮所有记录、状态、会话保存、token用量回调
        prompt_state = request.prompt_state if request.prompt_state is not None else PromptState()
        turn = LeadTurn(request.history, request.save_context, request.on_usage, prompt_state)
        # 实例化恢复状态对象：保存本轮API重试、模型降级、上下文回滚恢复相关状态
        state = recovery.RecoveryState()
        # 手动压缩管理器，管控手动触发的工具返回内容压缩以及压缩执行时机
        manual_compaction = compaction.ManualCompaction()
        # 开启自动上下文压缩开关，当上下文过长时自动压缩对话历史
        automatic_compaction_enabled = True
        retry_request = None
        active_request = request.active_request

        # -- 准备上下文并请求完整模型响应 --
        # 每次循环迭代：调用一次模型，处理模型返回的文本回答 或者 工具调用请求
        while True:
            # 检查外部停止信号（用户主动终止、运行时关闭），收到信号就结束本轮
            if interaction.stopped():
                return self._finish(turn, "stopped")

            self._set_phase(TurnPhase.PREPARING)
            # 标记模型调用已经开始，用于异常捕获、重试逻辑判断
            model_call_started = False
            try:
                if retry_request is None:
                    active_request = turn.inject_scheduled(active_request)
                    # 将已完成的后台任务通知加入对话历史。
                    turn.inject_background_notes()
                    # 一次准备固定 system、动态通知与工具快照，计量和发送复用实际请求。
                    context = self.callbacks.build_context(turn.history)
                    tools, handlers = self.callbacks.assemble_tool_pool()
                    previous_prompt = prompt_state.snapshot()
                    # 按上下文预算裁剪或压缩历史消息，包含将要追加的运行上下文。
                    system, compacted = prompt_state.prepare(
                        turn.history,
                        context.prompt_values(),
                        prompts.assemble_system_prompt,
                        active_request,
                        enabled=automatic_compaction_enabled,
                        client=core.client,
                        model=state.current_model,
                        tools=tools,
                    )
                    # 快照、通知和压缩后的历史在同一检查点保存，重试不重复追加。
                    if compacted.applied or prompt_state.snapshot() != previous_prompt:
                        turn.checkpoint()
                    if compacted.reason:
                        automatic_compaction_enabled = False
                        core.terminal_print(
                            f"[compact error] Automatic compaction disabled for this turn: {compacted.reason}"
                        )
                else:
                    # 输出截断升级与网络重试都复用同一请求，不重新检索记忆或追加状态。
                    system, tools, handlers = retry_request

                if interaction.stopped():
                    return self._finish(turn, "stopped")

                self._set_phase(TurnPhase.WAITING_MODEL)
                model_call_started = True
                # 重试策略和模型降级由 call_llm 内部处理。
                turn.begin_model_call()
                response, call_id, operation_id = self.callbacks.call_llm(
                    turn.history, system, tools, state
                )
            # 准备阶段或模型调用失败时，归还尚未确认的定时任务。
            except Exception as exc:
                if isinstance(exc, StreamStopped):
                    return self._finish(turn, "stopped")
                # 准备阶段错误不消耗模型上下文恢复机会。
                # 首次遇到提示过长错误时压缩历史，再重新调用模型。
                if (
                    not interaction.stopped()
                    and model_call_started
                    and recovery.is_prompt_too_long_error(exc)
                    and state.claim_reactive_compaction()
                ):
                    # 原地替换历史，保留调用方持有的列表引用。
                    compacted = compaction.reactive_compact(turn.history, active_request)
                    turn.after_compaction(compacted)
                    if compacted.reason is None:
                        retry_request = None
                        continue
                    exc = RuntimeError(f"Reactive compaction failed: {compacted.reason}")
                # 结束本轮交互，不继续执行工具。
                return self._finish(turn, "failed", f"{type(exc).__name__}: {exc}")

            # 模型已成功响应，确认本批定时任务已交付。
            self._set_phase(TurnPhase.PROCESSING_RESPONSE)
            turn.acknowledge_scheduled()
            action = turn.accept_response(response, call_id, operation_id, state)

            # -- 接受工具请求前，先处理输出截断的重试与续写 --
            if interaction.stopped() and action in {"retry", "continue", "stop"}:
                # 截断的工具请求不能执行或保存，只保留可用文本。
                text = message_content.extract_text(response.content)
                if text:
                    turn.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
                return self._finish(turn, "stopped")

            if action == "retry":
                core.terminal_print(f"  \033[33m[max_tokens] retry with {state.max_tokens}\033[0m")
                retry_request = system, tools, handlers
                continue

            retry_request = None
            if action in {"continue", "stop"}:
                # 输出额度升级的请求返回后，才保留可续写的部分文本。
                turn.append({"role": "assistant", "content": response.content})
                if action == "continue":
                    # 添加续写提示，作为下一次模型调用的用户消息。
                    turn.append({"role": "user", "content": core.CONTINUATION_PROMPT})
                    # 返回循环顶部继续调用模型。
                    continue
                # 续写机会耗尽时释放已完成的分配记录。
                # 停止本轮交互。
                return self._finish(turn, "truncated", "Output continuation limit reached")

            # 保存模型响应，供工具结果和后续调用引用。
            turn.append({"role": "assistant", "content": response.content})

            # -- 判断目标是否允许本轮回答结束 --
            # 没有工具请求时，模型已完成当前一轮回答。
            if not message_content.has_tool_use(response.content):
                if interaction.stopped():
                    return self._finish(turn, "stopped")
                # 通知 Stop 钩子本轮模型输出已完成。
                hooks.trigger_hooks("Stop", turn.history)
                if interaction.stopped():
                    return self._finish(turn, "stopped")
                # 检查当前目标是否允许结束。
                decision = self.callbacks.evaluate_goal(turn.history)
                turn.after_goal_evaluation(decision)
                if interaction.stopped():
                    return self._finish(turn, "stopped")
                # 目标未达成时，反馈缺失条件并继续调用模型。
                if decision is not None and decision.action == "block":
                    # 读取仍在生效的目标条件。
                    condition = self.callbacks.goal_condition()
                    # 在终端显示要求继续的原因。
                    core.terminal_print(f"  \033[35m[goal] continue: {decision.reason[:80]}\033[0m")
                    if interaction.stopped():
                        return self._finish(turn, "stopped")
                    # 将目标条件和评估结果写回历史。
                    turn.append({
                        "role": "user",
                        "content": (
                            "[Goal still active]\n"
                            f"Condition: {condition}\n"
                            f"Evaluator: {decision.reason}\n"
                            "Continue working and surface the missing evidence."
                        ),
                    })
                    # 再次调用模型寻找缺失证据。
                    continue
                # 对非 allow 的其他评估结果仅输出提示。
                if decision is not None and decision.action != "allow":
                    # 在终端显示评估结果和原因。
                    core.terminal_print(f"  \033[35m[goal] {decision.action}: {decision.reason}\033[0m")
                # 从已完成的对话中提取并整理长期记忆。
                self.callbacks.remember_after_turn(turn.history)
                if interaction.stopped():
                    return self._finish(turn, "stopped")
                # 释放已完成任务的 agent 分配记录。
                # 本轮回答结束。
                return self._finish(turn, "completed")

            # -- 逐项结算工具，再将完整结果批次交给模型 --
            # 当前阶段由 TurnEngine 管理，工具函数只负责执行和记录结果。
            self._set_phase(TurnPhase.EXECUTING_TOOLS)
            results = execute_tool_batch(response.content, handlers, manual_compaction, turn)
            # 把所有工具结果及已完成的后台通知写入历史。
            turn.append({"role": "user", "content": self.callbacks.build_user_content(results)})
            if interaction.stopped():
                return self._finish(turn, "stopped")
            # 如本轮请求了手动压缩，在工具结果回填后生成摘要。
            turn.after_manual_compaction(
                manual_compaction.apply_after_results(turn.history, active_request)
            )
