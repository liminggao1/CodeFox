"""Lead 回合策略：定时任务交付、模型恢复、Goal/memory 与结束收尾。

TurnEngine 决定执行顺序并写入历史；本模块持有 Lead 的恢复状态和定时任务，
通过执行机提供的检查点保存宿主记录，不直接写会话文件，也不渲染前端。
"""

from .. import core
from ..context import compaction
from ..runtime import hooks, interaction, scheduler
from ..runtime.session_store import json_value
from ..workspace import tasks
from . import recovery
from .lead_tools import execute_tool


class LeadTurn:
    publish_text = True
    preserve_tail = False

    def __init__(self, history, checkpoint=None, prompt_state=None, *,
                 evaluate_goal=None, remember_after_turn=None, goal_condition=None):
        # 本回合唯一的历史句柄：调用方不再持有同一列表的第二个名字。
        self.history = history
        self._checkpoint = checkpoint
        self.prompt_state = prompt_state
        self.engine = None
        self.client = core.client
        # 保存本轮 API 重试、模型降级和唯一一次上下文恢复的状态。
        self.recovery = recovery.RecoveryState()
        # 手动压缩在整批工具结果回填后执行。
        self.manual_compaction = compaction.ManualCompaction()
        self.automatic_compaction_enabled = True
        self._evaluate_goal = evaluate_goal or (lambda history: None)
        self._remember_after_turn = remember_after_turn or (lambda history: None)
        self._goal_condition = goal_condition or (lambda: "")
        # 暂存已取出、尚未确认交付的定时任务。
        self._pending_cron = []

    @property
    def model(self):
        return self.recovery.current_model

    @property
    def continuation_prompt(self):
        return core.CONTINUATION_PROMPT

    def stopped(self):
        return interaction.stopped()

    def checkpoint(self):
        if self.engine is not None:
            self.engine.checkpoint()
        elif self._checkpoint is not None:
            self._checkpoint()

    def before_iteration(self, active_request):
        active_request = self.inject_scheduled(active_request)
        # 将已完成的后台任务通知加入对话历史。
        self.inject_background_notes()
        return active_request

    def after_preparation(self, compacted):
        if compacted.reason:
            self.automatic_compaction_enabled = False
            core.terminal_print(
                f"[compact error] Automatic compaction disabled for this turn: {compacted.reason}"
            )

    def recover_context(self, active_request):
        if not self.recovery.claim_reactive_compaction():
            return False
        # 原地替换历史，保留调用方持有的列表引用。
        compacted = compaction.reactive_compact(self.history, active_request)
        self.after_compaction(compacted)
        if compacted.reason is not None:
            raise RuntimeError(f"Reactive compaction failed: {compacted.reason}")
        return True

    def accept_response(self, response, on_usage=None):
        """模型成功响应，确认定时任务交付并登记用量和恢复决定。"""
        self.acknowledge_scheduled()
        usage = getattr(response, "usage", None)
        if usage is not None:
            usage = json_value(usage)
            if on_usage is not None:
                on_usage(usage)
            interaction.publish("lead_usage", usage=usage)
        action = self.recovery.accept_response(getattr(response, "stop_reason", None))
        if action == "retry":
            core.terminal_print(f"  \033[33m[max_tokens] retry with {self.recovery.max_tokens}\033[0m")
        return action

    def execute_tool(self, block, handlers):
        """只结算单个 Lead 工具；批次顺序和结果配对交给共享执行机。"""
        return execute_tool(block, handlers, self.manual_compaction)

    def after_tools(self, response, results, active_request, *, error=None):
        if error is None and not self.stopped():
            # 如本轮请求了手动压缩，在工具结果回填后生成摘要。
            self.after_manual_compaction(
                self.manual_compaction.apply_after_results(self.history, active_request)
            )

    def complete(self):
        """检查目标是否允许结束；阻止结束时返回下一轮用户反馈。"""
        # 通知 Stop 钩子本轮模型输出已完成。
        hooks.trigger_hooks("Stop", self.history)
        if self.stopped():
            return None
        # 检查当前目标是否允许结束，并保存评估边界。
        decision = self._evaluate_goal(self.history)
        self.after_goal_evaluation(decision)
        if self.stopped():
            return None
        # 目标未达成时，反馈缺失条件并继续调用模型。
        if decision is not None and decision.action == "block":
            condition = self._goal_condition()
            core.terminal_print(f"  \033[35m[goal] continue: {decision.reason[:80]}\033[0m")
            return ("[Goal still active]\n"
                    f"Condition: {condition}\n"
                    f"Evaluator: {decision.reason}\n"
                    "Continue working and surface the missing evidence.")
        # 对非 allow 的其他评估结果仅输出提示。
        if decision is not None and decision.action != "allow":
            core.terminal_print(f"  \033[35m[goal] {decision.action}: {decision.reason}\033[0m")
        # 从已完成的对话中提取并整理长期记忆。
        self._remember_after_turn(self.history)
        return None

    # -- 上下文变更后的检查点 --

    def after_compaction(self, compacted):
        """被动恢复真正改写了历史才重置快照并落盘；失败不改变提示词状态。"""
        if compacted.applied:
            if self.prompt_state is not None:
                self.prompt_state.reset()
            self.checkpoint()

    def after_manual_compaction(self, result):
        """手动压缩执行完成就落盘；本轮未触发手动压缩（None）时不动。"""
        if result is not None:
            if result.applied and self.prompt_state is not None:
                self.prompt_state.reset()
            self.checkpoint()

    def after_goal_evaluation(self, decision):
        """目标评估产生了决定就落盘，保留评估边界。"""
        if decision is not None:
            self.checkpoint()

    # -- 定时任务交付与回合收尾 --

    def inject_background_notes(self):
        """把已完成的后台通知写入历史；没有通知时不写历史也不保存。"""
        try:
            notes = scheduler.collect_background_results()
        except OSError as exc:
            core.terminal_print(f"[background error] Could not save results: {exc}")
            return
        if notes:
            message = {"role": "user", "content": [
                {"type": "text", "text": note} for note in notes]}
            if self.engine is not None:
                self.engine.append(message)
            else:
                self.history.append(message)
                self.checkpoint()

    def inject_scheduled(self, active_request):
        """注入定时任务，并保留待确认清单直到模型成功响应。"""
        # 从队列中取出本次到期的定时任务。
        fired = scheduler.consume_cron_queue()
        # 调用模型成功前保留这些任务，以便失败时重新入队。
        self._pending_cron.extend(fired)
        for job in fired:
            # 将每个定时任务作为用户消息交给模型，显示截短的注入提示。
            self.engine.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            core.terminal_print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
        if fired:
            # 将本批定时任务合并成多行请求，也纳入压缩摘要的当前请求。
            scheduled = "\n".join(f"Run scheduled task: {job.prompt}" for job in fired)
            active_request = f"{active_request}\n{scheduled}".strip()
        return active_request

    def acknowledge_scheduled(self):
        try:
            # 将已消费的任务从持久化记录中确认移除。
            scheduler.acknowledge_cron_jobs(self._pending_cron)
        except OSError as exc:
            # 确认写入失败不阻止处理模型响应。
            core.terminal_print(f"[cron error] Delivery confirmation failed: {exc}")
        self._pending_cron.clear()

    def finish(self, status, reason="", *, response=None, error=None):
        """归还未确认交付，再释放 Lead 已完成任务，保持 TurnOutcome 契约。"""
        if status in {"stopped", "failed"}:
            scheduler.restore_cron_jobs(self._pending_cron)
        if status == "stopped":
            reason = "Stop requested"
        tasks.release_completed_assignment("agent")
        return interaction.TurnOutcome(status, reason)
