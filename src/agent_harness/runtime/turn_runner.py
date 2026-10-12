"""一次交互提交的唯一拥有者：排队、并发互斥、停止信号与终态结算。

``InteractiveRuntime`` 只保留执行线程、生命周期标志与装配；待执行提交、
进行中回合、停止信号、后台暂停标志与 lead 用量账本都在这里。命令与普通
回合共用同一条终态结算路径：``_execute`` 的步骤整体搬入，顺序、发布时机
与保存次数保持不变。
"""

from __future__ import annotations

from dataclasses import asdict, replace
import threading
import time
import uuid

from ..agents import subagent as subagent_runtime
from . import hooks, scheduler
from . import interaction
from .local_commands import find_command


class TurnRunner:
    """提交排队与回合执行；依赖由运行时显式注入，不反向引用 InteractiveRuntime。"""

    def __init__(self, *, lifecycle, is_started, is_closing, session, host, dispatch,
                 context, checkpoint, record, snapshot, complete_event):
        self._lifecycle = lifecycle
        self._is_started = is_started
        self._is_closing = is_closing
        self._session = session
        # host 模块引用由运行时注入：两边必须指向同一份模块（测试会重载它）。
        self._host = host
        # 命令路由由运行时晚绑定传入：CommandRouter 依赖 SessionSwitch，
        # 而 SessionSwitch 依赖本运行时的回调，直接注入会形成构造环。
        self._dispatch = dispatch
        self._context = context
        self._checkpoint = checkpoint
        self._record = record
        self._snapshot = snapshot
        self._complete_event = complete_event
        self._pending = None
        self._busy = False
        self._turn = None
        self._stop = None
        self._background_paused = False
        self._usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
        self._last_input_tokens = 0
        self._turn_started_at = None
        self._turn_usage = dict(self._usage)

    # -- 运行时读取的状态 --

    @property
    def busy(self):
        return self._busy

    @property
    def background_paused(self):
        """后台回合是否被暂停；供运行时快照与前端断言读取。"""
        return self._background_paused

    def usage_snapshot(self):
        """当前 lead 累计用量；会话切换后由运行时重置。"""
        return dict(self._usage)

    def last_input_tokens(self):
        """Return the most recent model request input size for the UI summary."""
        return self._last_input_tokens

    def reset_usage(self):
        """会话切换落地后清零累计用量。"""
        self._usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
        self._last_input_tokens = 0

    def record_usage(self, usage):
        """累计一次模型响应的输入与输出用量。"""
        value = usage.get("input_tokens", 0) or 0
        if isinstance(value, int) and value >= 0:
            self._last_input_tokens = value
        for key in ("input_tokens", "output_tokens"):
            self._usage[key] += usage.get(key, 0) or 0

    def count_model_call(self):
        """记一次已开始的模型调用；运行时保存执行状态时触发。"""
        self._usage["model_calls"] += 1

    # -- 排队、停止与后台占用 --

    def admit(self, text):
        """排队一次提交；被拒绝时不改变任何状态。"""
        if not isinstance(text, str) or not text.strip():
            return interaction.SubmissionResult("invalid", reason="Empty input")
        text = text.strip()
        with self._lifecycle:
            if self._is_closing():
                return interaction.SubmissionResult("closed", reason="Runtime is closing")
            if not self._is_started():
                return interaction.SubmissionResult("invalid", reason="Runtime is not started")
            if self._busy:
                return interaction.SubmissionResult("busy", reason="A turn or command is running")
            kind = "command" if find_command(text.split()[0]) else "turn"
            submission_id = uuid.uuid4().hex
            self._busy = True
            self._pending = (submission_id, kind, text)
            self._background_paused = False
            return interaction.SubmissionResult("accepted", submission_id, kind)

    def request_stop(self, turn_id):
        """请求停止当前回合；回合不匹配时返回 False，不改变状态。"""
        with self._lifecycle:
            if self._turn != turn_id:
                return False
            self._stop.set()
            self._background_paused = True
            return True

    def cancel_active(self):
        """关闭时终止进行中的回合，返回被终止的 turn_id（无回合时为 None）。"""
        with self._lifecycle:
            if self._stop is None:
                return None
            self._stop.set()
            return self._turn

    def pause_background(self):
        """暂停后台回合，避免重复消费同一批损坏输入。"""
        with self._lifecycle:
            self._background_paused = True

    def take_pending(self):
        """取出待执行提交；无提交时返回 None。"""
        with self._lifecycle:
            pending, self._pending = self._pending, None
            return pending

    def reserve_background(self):
        """空闲且后台未被暂停时占用一个后台回合；返回是否占用成功。"""
        with self._lifecycle:
            if self._busy or self._background_paused:
                return False
            self._busy = True
            return True

    def abandon_pending(self):
        """关闭时结算尚未开始的提交，避免前端等不到终态。"""
        with self._lifecycle:
            pending, self._pending = self._pending, None
            if pending is None:
                return
            submission_id, kind, text = pending
            context = self._context()
            if kind == "command":
                context.publish("command_result", submission_id=submission_id,
                                command=text.split()[0], status="failed",
                                data={"error": "Runtime is closing"})
                return
            context = replace(context, turn_id=uuid.uuid4().hex)
            context.publish("turn_started", submission_id=submission_id)
            self._complete_event(context, "turn_finished", submission_id=submission_id,
                                 outcome=asdict(interaction.TurnOutcome("stopped", "Runtime is closing")),
                                 summary={"elapsed_seconds": 0, "tool_calls": 0, "approvals": 0,
                                          "lead_usage": {"model_calls": 0, "input_tokens": 0,
                                                         "output_tokens": 0}})

    # -- 回合执行 --

    def run(self, submission_id, kind, text):
        """执行一次提交；命令与回合共用同一终态结算路径。"""
        with self._host.agent_lock:
            context = self._context(origin="frontend" if kind != "background" else "background")
            try:
                if kind == "command":
                    with interaction.execution_context(context):
                        outcome = self._dispatch(text)
                    if outcome.followup is None:
                        with self._lifecycle:
                            self._busy = False
                        self._snapshot(self._context())
                        context = self._context()
                        self._complete_event(context, "command_result", submission_id=submission_id,
                                             command=outcome.command, status="completed", data=outcome.data)
                        return
                    text = outcome.followup
                with self._lifecycle:
                    self._turn = uuid.uuid4().hex
                    self._turn_started_at = time.monotonic()
                    self._turn_usage = dict(self._usage)
                    self._stop = threading.Event()
                    if self._is_closing():
                        self._stop.set()
                    context = self._context(turn_id=self._turn,
                                            origin="background" if kind == "background" else "frontend",
                                            stop_event=self._stop)
                with interaction.execution_context(context):
                    self._session.finish_pending_tools()
                    if kind == "background":
                        inbox = subagent_runtime.consume_lead_inbox(route_protocol=True)
                        if inbox:
                            self._session.history.append({"role": "user", "content": subagent_runtime.format_subagent_events(inbox)})
                        prompts = scheduler.pending_cron_prompts()
                        if prompts:
                            text = "\n".join(f"Run scheduled task: {prompt}" for prompt in prompts)
                        self._record("background_notification", text, save=False)
                    else:
                        self._session.history.append({"role": "user", "content": text})
                        self._record("user_input", text, save=False)
                    # 首条用户消息可能创建了持久会话 ID。
                    context = replace(context, session_id=self._session.session_id)
                with interaction.execution_context(context):
                    if kind == "command":
                        context.publish("command_result", submission_id=submission_id,
                                        command=outcome.command, status="completed", data=outcome.data,
                                        turn_id=context.turn_id, recorded=True)
                    if kind != "background":
                        context.publish("user_message", text=text, submission_id=submission_id)
                        hooks.trigger_hooks("UserPromptSubmit", text)
                    context.publish("turn_started", submission_id=submission_id)
                    self._checkpoint()
                    self._snapshot(context)
                    try:
                        outcome = self._host.agent_loop(self._session.history, text, self._checkpoint,
                                                        self.record_usage)
                    except Exception as exc:
                        outcome = interaction.TurnOutcome("failed", f"{type(exc).__name__}: {exc}")
                    with self._lifecycle:
                        if self._stop.is_set() and outcome.status != "failed":
                            outcome = interaction.TurnOutcome("stopped", "Stop requested")
                        self._turn = None
                        self._stop = None
                    self._checkpoint(poll=True)
                    summary = self._turn_summary(context, outcome)
                    self._record("turn_complete", text, outcome.status, save=False)
                    with self._lifecycle:
                        self._busy = False
                    self._snapshot(context)
                    self._complete_event(context, "turn_finished", submission_id=submission_id,
                                         outcome=asdict(outcome), summary=summary)
            except Exception as exc:
                with self._lifecycle:
                    self._busy = False
                    self._turn = None
                    self._stop = None
                if kind == "command":
                    # 命令可能已经激活了另一个会话。
                    context = self._context()
                self._snapshot(context)
                self._complete_event(context, "command_result" if kind == "command" else "turn_finished",
                                submission_id=submission_id, command=text.split()[0],
                                status="failed", data={"error": str(exc)},
                                outcome=asdict(interaction.TurnOutcome("failed", str(exc))),
                                summary=self._turn_summary(context))

    def _turn_summary(self, context, outcome=None):
        records = self._session.display_records()
        tool_calls = sum(1 for record in records
                         if record["type"] == "tool_call" and record["turn_id"] == context.turn_id)
        approvals = sum(1 for record in records
                        if record["type"] == "approval_requested" and record["turn_id"] == context.turn_id)
        return {"elapsed_seconds": round(time.monotonic() - self._turn_started_at, 3)
                if self._turn_started_at is not None else 0,
                "tool_calls": tool_calls, "approvals": approvals,
                "lead_usage": {key: self._usage[key] - self._turn_usage[key] for key in self._usage}}
