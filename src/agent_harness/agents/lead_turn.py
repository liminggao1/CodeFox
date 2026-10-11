"""Lead 回合记录：本回合的会话历史、展示事实与结束收尾。

host 决定执行与恢复顺序，只经 ``turn.history`` 读写历史。本模块维护本轮
关联状态与落盘时机，通过运行时提供的检查点保存记录；不直接写会话文件，
也不渲染前端。
"""

from .. import core
from ..runtime import interaction, scheduler
from ..runtime.session_store import json_value
from ..workspace import tasks


class LeadTurn:
    def __init__(self, history, checkpoint=None, on_usage=None):
        # 本回合唯一的历史句柄：调用方不再持有同一列表的第二个名字。
        self.history = history
        self._checkpoint = checkpoint
        self._on_usage = on_usage
        self._model_call_id = None
        self._text_indices = {}
        # 暂存已取出、尚未确认交付的定时任务。
        self._pending_cron = []

    @property
    def model_call_id(self):
        return self._model_call_id

    def checkpoint(self):
        if self._checkpoint is not None:
            self._checkpoint()

    # -- 历史写入、落盘时机与逐项结算的工具结果 --

    def append(self, message):
        """追加模型历史，发布已接受的助手消息块，再触发保存检查点。"""
        self.history.append(message)
        if message.get("role") == "assistant":
            identity = self._model_identity()
            for index, block in enumerate(json_value(message["content"])):
                if block.get("type") == "text":
                    interaction.publish("assistant_message", text=block["text"],
                                        block_index=self._text_indices.get(index, index), **identity)
                elif block.get("type") == "tool_use":
                    interaction.publish("tool_call", **block, **identity)
        self.checkpoint()

    def inject_background_notes(self):
        """把已完成的后台通知写入历史；没有通知时不写历史也不保存。"""
        try:
            notes = scheduler.collect_background_results()
        except OSError as exc:
            core.terminal_print(f"[background error] Could not save results: {exc}")
            return
        if notes:
            self.append({"role": "user", "content": [
                {"type": "text", "text": note} for note in notes]})

    def after_compaction(self, compacted):
        """自动压缩真正改写了历史才落盘；未应用的结果不产生额外保存。"""
        if compacted.applied:
            self.checkpoint()

    def after_manual_compaction(self, result):
        """手动压缩执行完成就落盘；本轮未触发手动压缩（None）时不动。"""
        if result is not None:
            self.checkpoint()

    def after_goal_evaluation(self, decision):
        """目标评估产生了决定就落盘，保留评估边界。"""
        if decision is not None:
            self.checkpoint()

    def record_tool_result(self, result, status):
        """主循环追加完整结果批次前，先逐项发布和保存真实工具结果。

        后续追加 user 侧批次时不重复发布。进程在工具之间中断，恢复时
        可以复用已结算项，仅为缺失项补充中断结果。
        """
        interaction.publish("tool_result", **json_value(result), status=status,
                            **self._model_identity())
        self.checkpoint()

    def _model_identity(self):
        return {"model_call_id": self._model_call_id} if self._model_call_id else {}

    # -- 模型调用关联与用量记录 --

    def begin_model_call(self):
        """清除上次调用关联，避免新请求失败时沿用上次已接受的调用 ID。"""
        self._model_call_id = None
        self._text_indices = {}

    def accept_response(self, response, call_id, operation_id, recovery_state):
        """记录完整响应的关联与用量，并登记 recovery 给出的恢复决定。"""
        self._model_call_id = call_id
        self._text_indices = getattr(response, "stream_indices", {})
        usage = getattr(response, "usage", None)
        if usage is not None:
            usage = json_value(usage)
            if self._on_usage is not None:
                self._on_usage(usage)
            interaction.publish("lead_usage", usage=usage)
        action = recovery_state.accept_response(response.stop_reason)
        if call_id:
            status = ("discarded" if action == "retry" else
                      "truncated" if action in {"continue", "stop"} else "completed")
            interaction.operation_status("model", "请求模型", status,
                                         operation_id=operation_id, model_call_id=call_id)
        return action

    # -- 定时任务交付与回合收尾 --

    def inject_scheduled(self, active_request):
        """注入定时任务，并保留待确认清单直到模型成功响应。"""
        # 从队列中取出本次到期的定时任务。
        fired = scheduler.consume_cron_queue()
        # 调用模型成功前保留这些任务，以便失败时重新入队。
        self._pending_cron.extend(fired)
        # 将每个定时任务作为一条用户消息送入对话历史。
        for job in fired:
            # 模型通过用户消息读取定时任务内容。
            self.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            # 在终端显示截短的注入提示。
            core.terminal_print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
        # 有定时任务时也把它们纳入压缩摘要使用的当前请求。
        if fired:
            # 将本批定时任务合并成多行请求。
            scheduled = "\n".join(f"Run scheduled task: {job.prompt}" for job in fired)
            # 保留原请求，并追加定时任务内容。
            active_request = f"{active_request}\n{scheduled}".strip()
        return active_request

    def acknowledge_scheduled(self):
        try:
            # 将已消费的任务从持久化记录中确认移除。
            scheduler.acknowledge_cron_jobs(self._pending_cron)
        # 确认写入失败不阻止处理模型响应。
        except OSError as exc:
            # 在终端报告确认失败。
            core.terminal_print(f"[cron error] Delivery confirmation failed: {exc}")
        # 清空本轮待确认清单。
        self._pending_cron.clear()

    def finish(self, status, reason=""):
        """统一回合收尾：先归还未确认交付，再记录失败并释放已完成任务。"""
        if status in {"stopped", "failed"}:
            # 调用最终失败时，将未确认的定时任务重新放回队列。
            scheduler.restore_cron_jobs(self._pending_cron)
        if status == "stopped":
            reason = "Stop requested"
        # 释放已完成任务的 agent 分配记录。
        tasks.release_completed_assignment("agent")
        return interaction.TurnOutcome(status, reason)
