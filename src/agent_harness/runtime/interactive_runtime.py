"""One serialized interactive driver shared by CLI and Textual adapters."""

from __future__ import annotations

import threading

from .. import core
from .. import host
from ..context import compaction
from ..workspace import tasks
from ..agents import subagent as subagent_runtime
from ..tools import file_tools
from . import scheduler
from . import interaction, permissions
from .commands import CommandRouter
from .lead_context import LeadContext
from .session_switch import SessionSwitch
from .session_store import InteractiveSession, SessionStore, json_value
from .turn_runner import TurnRunner


class InteractiveRuntime:
    def __init__(self, *, channel=None, store=None):
        self.channel = channel or interaction.InteractionChannel()
        self.lead_context = LeadContext(tasks.task_lock)
        self.channel.set_projector(self._display_record)
        self.session = InteractiveSession(store or SessionStore(core.WORKDIR), [],
                                          lambda: host.GOAL, core.terminal_print,
                                          self._session_context)
        self._lifecycle = threading.RLock()
        self._wake = threading.Event()
        self._closing = False
        self._started = False
        self._generation = 1
        self._thread = None
        # 提交排队、回合互斥、停止信号与用量由回合执行 module 独占。
        self.turns = TurnRunner(
            lifecycle=self._lifecycle, is_started=lambda: self._started,
            is_closing=lambda: self._closing, session=self.session, host=host,
            dispatch=self._dispatch_command, context=self._context,
            checkpoint=self._checkpoint, record=self._record,
            snapshot=self._snapshot, complete_event=self._complete_event)
        # 会话切换事务由独立 module 拥有；这里只注入运行时自己的窄接口。
        self.session_switch = SessionSwitch(
            store=self.session.store, session=self.session,
            lead_context=self.lead_context, channel=self.channel,
            context=self._context, checkpoint=self._checkpoint,
            record=self._record, pause_background=self._pause_background,
            after_activate=self._after_activate,
            read_goal=self._read_goal, replace_goal=self._replace_goal)
        self.command_router = CommandRouter(
            store=self.session.store, session_id=lambda: self.session.session_id,
            read_goal=self._read_goal, record=self._record, close=self.close,
            session_switch=self.session_switch, new_session=self.session_switch.start_new,
            compact_history=self._compact_history,
            active_request=lambda: self.session.active_request)

    def _compact_history(self, active_request):
        """用户主动压缩当前会话历史；成功后由终态结算统一保存。"""
        return compaction.manual_compact(self.session.history, active_request)

    def _dispatch_command(self, text):
        """CommandRouter 晚绑定回调：它依赖 SessionSwitch，直接注入会形成构造环。"""
        return self.command_router.dispatch(text)

    def _session_context(self):
        with tasks.task_lock:
            return self.lead_context.snapshot(tasks.active_lead_task_id())

    def _record(self, *args, **kwargs):
        # 首次创建明确激活归属，保存只读取状态，不暗中修改 session_id。
        if self.session.snapshot is None:
            snapshot = self.session.store.create()
            snapshot["messages"] = json_value(self.session.history)
            with tasks.task_lock:
                self.lead_context.activate(snapshot["id"], snapshot)
                tasks.activate_lead_assignment()
                self.session.activate(snapshot)
        return self.session.record(*args, **kwargs)

    def _context(self, **kwargs):
        return interaction.ExecutionContext(self.channel, self.session.session_id,
                                              self._generation, checkpoint=self._checkpoint,
                                              lead_context=self.lead_context,
                                              permission_mode=permissions.SETTINGS.mode,
                                              **kwargs)

    @property
    def permission_mode(self):
        return permissions.SETTINGS.mode

    def set_permission_mode(self, mode):
        """空闲时持久化并切换模式；提交与后台占用使用同一把锁。"""
        with self._lifecycle:
            if self._closing or self.turns.busy:
                raise ValueError("当前正在执行，不能切换权限模式")
            permissions.SETTINGS.save(mode)

    def start(self):
        with self._lifecycle:
            if self._started or self._closing:
                return
            host.initialize_runtime()
            self._started = True
            self._thread = threading.Thread(target=self._run, name="interactive-lead", daemon=True)
            self._thread.start()

    def submit(self, text: str) -> interaction.SubmissionResult:
        result = self.turns.admit(text)
        if result.status == "accepted":
            self._wake.set()
        return result

    def request_stop(self, turn_id: str) -> bool:
        if not self.turns.request_stop(turn_id):
            return False
        self.channel.cancel_turn(turn_id)
        return True

    def close(self):
        with self._lifecycle:
            if self._closing:
                return
            self._closing = True
            turn_id = self.turns.cancel_active()
            if turn_id is not None:
                self.channel.cancel_turn(turn_id)
            self.channel.close_approvals()
            self._wake.set()

    def _snapshot(self, context):
        # All calls run on the execution thread while agent_lock is held.
        todos = self.lead_context.todos
        session_snapshot = self.session.snapshot or {}
        context.publish("state_snapshot", history=json_value(self.session.history),
                        goal=host.GOAL.snapshot(), todos=todos,
                        subagents=subagent_runtime.list_subagents(), model=core.MODEL,
                        busy=self.turns.busy, closing=self._closing,
                        permission_mode=self.permission_mode,
                        context_input_tokens=self.turns.last_input_tokens(),
                        context_limit_tokens=core.CONTEXT_LIMIT,
                        lead_main_model_usage=self.turns.usage_snapshot(),
                        workdir=session_snapshot.get("workdir"),
                        task_id=session_snapshot.get("task_id"),
                        worktree=session_snapshot.get("worktree"),
                        active_user_request=session_snapshot.get("active_user_request"))

    def _background_ready(self):
        if self.session.session_id is None:
            return False
        return (scheduler.has_pending_cron() or subagent_runtime.has_pending_inbox()
                or scheduler.has_pending_background())

    @staticmethod
    def _display_record(fact):
        """Project accepted JSON facts without accessing runtime or business state."""
        if fact.session_id is None:
            return None
        if fact.type == "command_result" and "sessions" in fact.payload.get("data", {}):
            return None
        record = {"id": fact.id, "at": fact.at, "kind": "display",
                  "type": fact.type, "source": fact.source,
                  "turn_id": fact.turn_id, "payload": interaction.data_copy(fact.payload)}
        if fact.type == "tool_result":
            record["payload"].setdefault("status", "failed" if fact.payload.get("is_error") else "returned")
        return record

    def _checkpoint(self, *, poll=False):
        facts = self.channel.drain_facts()
        changed = False
        for fact in facts:
            if (fact.session_id != self.session.session_id or fact.session_id is None
                    or fact.generation != self._generation):
                continue
            record = fact.payload["record"]
            if self.session.append_record(record):
                changed = True
                if (record["type"] == "execution_status" and record["source"] == "lead"
                        and record["payload"]["phase"] == "model" and record["payload"]["state"] == "running"):
                    self.turns.count_model_call()
        if poll and not changed:
            return True
        saved = self.session.save()
        context = interaction.current_context()
        if context is not None:
            self._snapshot(context)
        return saved

    def _complete_event(self, context, kind, **payload):
        """Save a terminal fact before exposing completion to the adapter."""
        recorded = self.session.session_id is not None and "sessions" not in payload.get("data", {})
        if kind == "turn_finished":
            outcome = payload.get("outcome", {})
            if outcome.get("status") == "failed" and outcome.get("reason"):
                # 错误卡是会话展示事实，不进入模型消息历史；在终态保存前发布，
                # 因而既能立即显示，也能随会话恢复。
                self.channel.publish(context, "system_error", {"error": outcome["reason"]})
        if recorded:
            self.channel.publish(context, kind, payload, deliver=False)
        with interaction.execution_context(context):
            saved = self._checkpoint()
        if kind == "turn_finished":
            payload["saved"] = saved
        if kind == "command_result":
            payload["recorded"] = recorded
        self.channel.publish(context, kind, payload, capture=False)
        return saved

    def _run(self):
        try:
            with host.agent_lock, interaction.execution_context(self._context()):
                self.lead_context.activate(None, {"worktree": None, "todos": []})
                tasks.activate_lead_assignment()
                scheduler.start_runtime_services()
                subagent_runtime.consume_lead_inbox(route_protocol=False)
                self._snapshot(self._context())
            while True:
                self._wake.wait(core.ASYNC_EVENT_POLL_SECONDS)
                self._wake.clear()
                background_ready = self._background_ready()
                with self._lifecycle:
                    if self._closing:
                        self.turns.abandon_pending()
                        break
                    work = self.turns.take_pending()
                    if work is None and background_ready and self.turns.reserve_background():
                        work = (None, "background", self.session.active_request)
                if work is not None:
                    self.turns.run(*work)
                else:
                    with host.agent_lock, interaction.execution_context(self._context()):
                        self._checkpoint(poll=True)
        except Exception as exc:
            context = self._context()
            error = f"{type(exc).__name__}: {exc}"
            context.publish("system_error", error=error)
            context.publish("runtime_error", error=error)
            self.close()
        finally:
            self._shutdown()

    # -- 会话切换已移入 session_switch.SessionSwitch；这里只提供它的窄回调。 --

    def _read_goal(self):
        """返回运行时当前持有的 goal 控制器。"""
        return host.GOAL

    def _replace_goal(self, value):
        """替换运行时持有的 goal 控制器。"""
        host.GOAL = value

    def _pause_background(self):
        """恢复后处理失败时暂停后台输入，避免重复消费同一批损坏数据。"""
        self.turns.pause_background()

    def _after_activate(self):
        """切换落地后刷新运行时自身的用量与代际。"""
        self.turns.reset_usage()
        self._generation += 1

    def _shutdown(self):
        self.channel.close_approvals()
        with host.agent_lock, interaction.execution_context(self._context()):
            saved = self._checkpoint()
            scheduler.stop_runtime_services()
            for child in subagent_runtime.list_subagents():
                if child["mode"] == "persistent":
                    subagent_runtime.run_request_shutdown(child["name"])
            core.terminal_print("Closing background execution")
        file_tools._stop_all_shell_processes()
        subagent_runtime.wait_for_subagents()
        scheduler.wait_for_background_tasks()
        with host.agent_lock, interaction.execution_context(self._context()):
            # 关闭不再请求模型，但仍结算后台真实结果、全文和待交付通知。
            collected = True
            try:
                notes = scheduler.collect_background_results()
                if notes:
                    self.session.finish_pending_tools()
                    self.session.history.append({"role": "user", "content": [
                        {"type": "text", "text": note} for note in notes]})
            except OSError as exc:
                collected = False
                error = f"Could not save background results: {exc}"
                self._context().publish("system_error", error=error)
                self._context().publish("runtime_error", error=error)
            saved = self._checkpoint() and collected
            self._context().publish("runtime_closed", saved=saved)
        self.channel.close()
