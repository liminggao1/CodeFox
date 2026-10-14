"""目标层：会话级目标状态与 Stop 边界。

模型停止请求工具时，host 调用 ``GoalController.evaluate_after_turn()``；
若目标判定为 ``block``，则继续当前循环。无工具模型评估由
``goal_evaluator.py`` 独立负责。
"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_EVALUATOR_MAX_TOKENS = 512
DEFAULT_STOP_HOOK_BLOCK_CAP = 8


MAX_GOAL_LENGTH = 4000
CLEAR_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}


class GoalError(Exception):
    """目标命令或评估器无法安全使用。"""


@dataclass
class GoalState:
    condition: str
    iterations: int
    set_at: float
    tokens_at_start: int
    last_reason: str | None = None


@dataclass(frozen=True)
class GoalEvaluation:
    ok: bool
    reason: str
    impossible: bool = False
    confidence: float | None = None


@dataclass(frozen=True)
class StopDecision:
    action: str
    reason: str = ""


class GoalController:
    """管理会话级目标状态和 Stop hook 的判定。"""

    def __init__(
        self,
        evaluator: Any,
        block_cap: int = DEFAULT_STOP_HOOK_BLOCK_CAP,
        events: list[dict[str, Any]] | None = None,
    ):
        if block_cap < 1:
            raise GoalError("block_cap must be at least 1")
        self.evaluator = evaluator
        self.block_cap = block_cap
        self.events = events if events is not None else []
        self.active: GoalState | None = None
        self.last_status: dict[str, Any] | None = None
        self.consecutive_blocks = 0

    def begin_query(self) -> None:
        self.consecutive_blocks = 0

    def set_goal(self, condition: str, tokens_at_start: int = 0) -> GoalState:
        """设置新的会话完成条件，并记录目标开始事件。

        输入：condition 为非空条件，去掉首尾空白后最多 4,000 字符；
            tokens_at_start 为开始时的 token 计数。
        返回：新 GoalState，同时替换 active 并清零连续阻止次数。
            已有目标会记录被替换事件；条件不合法时抛出 GoalError。
        """
        condition = condition.strip()
        if not condition:
            raise GoalError("goal condition cannot be empty")
        if len(condition) > MAX_GOAL_LENGTH:
            raise GoalError(
                f"goal condition cannot exceed {MAX_GOAL_LENGTH} characters"
            )
        if self.active is not None:
            self._record(
                active=False,
                met=False,
                failed=False,
                reason="replaced by a new goal",
            )
        self.active = GoalState(
            condition=condition,
            iterations=0,
            set_at=time.time(),
            tokens_at_start=tokens_at_start,
        )
        self.consecutive_blocks = 0
        self._record(active=True, met=False, failed=False, reason="goal set")
        return self.active

    def clear(self, reason: str = "cleared") -> str:
        """主动清除当前活动目标。

        输入：reason 为清除原因。
        返回：清除确认或无目标提示；记录事件并重置连续阻止计数，不调用评估器。
        """
        if self.active is None:
            return "未设置目标"
        condition = self.active.condition
        self._record(
            active=False,
            met=False,
            failed=False,
            reason=reason,
        )
        self.active = None
        self.consecutive_blocks = 0
        return f"已清除目标：{condition}"

    def status(self, current_tokens: int = 0) -> str:
        """生成可展示的目标状态文本。

        输入：current_tokens 为当前 token 计数，用于计算目标设置后的消费。
        返回：活动条件、耗时、评估次数和原因，或最近完成/失败状态；不更新目标。
        """
        if self.active is None:
            if self.last_status and self.last_status.get("met"):
                return (
                    f"目标已达成：{self.last_status['condition']}\n"
                    f"原因：{self.last_status.get('reason', '')}"
                )
            if self.last_status and self.last_status.get("failed"):
                return (
                    f"目标未达成：{self.last_status['condition']}\n"
                    f"原因：{self.last_status.get('reason', '')}"
                )
            return "未设置目标"
        elapsed = max(0, int(time.time() - self.active.set_at))
        spent = max(0, current_tokens - self.active.tokens_at_start)
        lines = [
            f"目标进行中：{self.active.condition}",
            f"已耗时：{elapsed}s",
            f"评估次数：{self.active.iterations}",
            f"已用 Token：{spent}",
        ]
        if self.active.last_reason:
            lines.append(f"上次原因：{self.active.last_reason}")
        return "\n".join(lines)

    async def evaluate_after_turn(
        self,
        messages: list[dict[str, Any]],
        background_running: bool = False,
    ) -> StopDecision:
        """评估本轮对话是否满足活动目标，并返回停止决定。
        Args:
            messages: 当前完整对话历史。
            background_running: 调用方是否要求延迟评估；host 传入已结束但尚未交付的
                后台结果是否存在，不代表仍有命令运行。
        Returns:
            StopDecision：无目标为 allow，需延迟为 defer；否则调用评估器。
            完成为 achieved、不可完成为 failed，并清空活动目标；未完成为 block，
            超过连续阻止上限为 limit；异常为 error，保留目标并记录原因。
        """
        # 没有活动目标时，不需要调用评估器，可以直接允许本轮结束。
        if self.active is None:
            return StopDecision("allow")
        # 后台任务尚未结束时，先延迟判断，避免用不完整的证据作结论。
        if background_running:
            return StopDecision(
                "defer", "background work is still running"
            )

        # 保存当前目标状态，后续评估和状态记录都使用同一个对象。
        state = self.active
        try:
            # 调用独立评估器，根据目标条件和对话历史生成判断结果。
            evaluation = await self.evaluator.evaluate(
                state.condition, messages
            )
        except Exception as error:
            # 将异常转换为可展示的原因，并保留目标继续活动的状态。
            reason = f"{type(error).__name__}: {error}"
            # 记录最近一次评估失败原因，供状态查询使用。
            state.last_reason = reason
            # 写入一条“评估出错但目标仍活动”的事件。
            self._record(
                active=True,
                met=False,
                failed=False,
                reason=reason,
            )
            # 告知调用方本次评估出错，但不把目标标记为已完成或失败。
            return StopDecision("error", reason)

        # 评估成功后，累计评估次数并保存评估器给出的说明。
        state.iterations += 1
        state.last_reason = evaluation.reason

        # ok=True 表示目标已经满足，可以关闭活动目标并允许结束。
        if evaluation.ok:
            # 记录目标已达成的最终事件。
            self._record(
                active=False,
                met=True,
                failed=False,
                reason=evaluation.reason,
                confidence=evaluation.confidence,
            )
            # 清除活动目标，避免下一轮重复评估同一个目标。
            self.active = None
            # 新目标开始前，清空连续阻止次数。
            self.consecutive_blocks = 0
            # 返回“已达成”，由 host 决定结束当前 Agent 回合。
            return StopDecision("achieved", evaluation.reason)

        # impossible=True 表示目标无法完成，应结束并标记为失败。
        if evaluation.impossible:
            # 记录目标无法完成的最终事件。
            self._record(
                active=False,
                met=False,
                failed=True,
                reason=evaluation.reason,
                confidence=evaluation.confidence,
            )
            # 清除已经失败的活动目标。
            self.active = None
            # 失败后同样重置连续阻止次数。
            self.consecutive_blocks = 0
            # 返回“失败”，由 host 结束当前 Agent 回合。
            return StopDecision("failed", evaluation.reason)

        # 目标尚未满足但仍可继续时，累计一次连续阻止。
        self.consecutive_blocks += 1
        # 记录目标仍活动以及本次缺失的完成证据。
        self._record(
            active=True,
            met=False,
            failed=False,
            reason=evaluation.reason,
            confidence=evaluation.confidence,
        )
        # 连续阻止超过上限后，不再无限循环等待模型补充证据。
        if self.consecutive_blocks > self.block_cap:
            # 返回“达到限制”，由 host 结束当前回合。
            return StopDecision(
                "limit",
                (
                    f"goal remains active, but the Stop hook blocked "
                    f"{self.block_cap} consecutive turns"
                ),
            )
        # 返回“阻止结束”，由 host 把原因写回对话并继续工作。
        return StopDecision("block", evaluation.reason)

    def _record(
        self,
        *,
        active: bool,
        met: bool,
        failed: bool,
        reason: str,
        confidence: float | None = None,
    ) -> None:
        state = self.active
        event = {
            "type": "goal_status",
            "condition": state.condition if state else "",
            "active": active,
            "met": met,
            "failed": failed,
            "reason": reason,
            "iterations": state.iterations if state else 0,
            "duration": (
                max(0, time.time() - state.set_at) if state else 0
            ),
        }
        if confidence is not None:
            event["confidence"] = confidence
        self.events.append(event)
        self.last_status = event

    def snapshot(self) -> dict[str, Any]:
        """Export session state without token counters or evaluator configuration."""
        active = None
        if self.active is not None:
            active = {key: getattr(self.active, key) for key in
                      ("condition", "set_at", "iterations", "last_reason")}
        return copy.deepcopy({"active": active,
                              "consecutive_blocks": self.consecutive_blocks,
                              "last_status": self.last_status,
                              "events": self.events})

    @classmethod
    def from_snapshot(cls, evaluator: Any, snapshot: dict,
                      block_cap: int = DEFAULT_STOP_HOOK_BLOCK_CAP) -> GoalController:
        """Validate and restore a full session snapshot using current configuration."""
        try:
            if not isinstance(snapshot, dict):
                raise ValueError("goal snapshot must be an object")
            blocks = snapshot["consecutive_blocks"]
            events, status = snapshot["events"], snapshot["last_status"]
            if (type(blocks) is not int or blocks < 0
                    or not isinstance(events, list)
                    or not all(isinstance(event, dict) for event in events)
                    or (status is not None and not isinstance(status, dict))):
                raise ValueError("invalid goal counters or events")
            if status is not None and (
                    not isinstance(status.get("condition"), str)
                    or type(status.get("met")) is not bool
                    or type(status.get("failed")) is not bool):
                raise ValueError("invalid last goal status")
            controller = cls(evaluator, block_cap, copy.deepcopy(events))
            controller.consecutive_blocks = blocks
            controller.last_status = copy.deepcopy(status)
            active = snapshot["active"]
            if active is not None:
                condition, iterations = active["condition"], active["iterations"]
                set_at, reason = active["set_at"], active["last_reason"]
                if (not isinstance(condition, str) or not condition.strip()
                        or len(condition) > MAX_GOAL_LENGTH
                        or type(iterations) is not int or iterations < 0
                        or type(set_at) not in (int, float) or not math.isfinite(set_at)
                        or (reason is not None and not isinstance(reason, str))):
                    raise ValueError("invalid active goal")
                controller.active = GoalState(condition, iterations, set_at, 0, reason)
            return controller
        except (KeyError, TypeError, ValueError) as exc:
            raise GoalError(f"Invalid goal snapshot: {exc}") from exc

    @classmethod
    def restore(
        cls,
        evaluator: Any,
        events: list[dict[str, Any]],
        block_cap: int = DEFAULT_STOP_HOOK_BLOCK_CAP,
    ) -> GoalController:
        controller = cls(
            evaluator=evaluator,
            block_cap=block_cap,
            events=list(events),
        )
        for event in reversed(events):
            if event.get("type") != "goal_status":
                continue
            controller.last_status = dict(event)
            if event.get("active"):
                controller.active = GoalState(
                    condition=str(event["condition"]),
                    iterations=0,
                    set_at=time.time(),
                    tokens_at_start=0,
                    last_reason=None,
                )
            break
        return controller
