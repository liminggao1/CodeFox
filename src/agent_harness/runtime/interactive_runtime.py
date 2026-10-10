"""One serialized interactive driver shared by CLI and Textual adapters."""

from __future__ import annotations

from dataclasses import asdict, replace
import threading
import time
import uuid

from .. import core
from ..agents import goal
from .. import host
from ..workspace import tasks
from ..agents import subagent as subagent_runtime
from ..tools import file_tools
from . import hooks, scheduler
from . import interaction
from .lead_context import LeadContext
from .local_commands import find_command
from .session_switch import SessionSwitch
from .session_store import InteractiveSession, SessionError, SessionStore, json_value


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
        self._busy = False
        self._pending = None
        self._turn = None
        self._stop = None
        self._background_paused = False
        self._generation = 1
        self._thread = None
        self._usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
        self._turn_started_at = None
        self._turn_usage = dict(self._usage)
        # 会话切换事务由独立 module 拥有；这里只注入运行时自己的窄接口。
        self.session_switch = SessionSwitch(
            store=self.session.store, session=self.session,
            lead_context=self.lead_context, channel=self.channel,
            context=self._context, checkpoint=self._checkpoint,
            record=self._record, pause_background=self._pause_background,
            after_activate=self._after_activate,
            read_goal=self._read_goal, replace_goal=self._replace_goal)

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
                                              **kwargs)

    def start(self):
        with self._lifecycle:
            if self._started or self._closing:
                return
            host.initialize_runtime()
            self._started = True
            self._thread = threading.Thread(target=self._run, name="interactive-lead", daemon=True)
            self._thread.start()

    def submit(self, text: str) -> interaction.SubmissionResult:
        if not isinstance(text, str) or not text.strip():
            return interaction.SubmissionResult("invalid", reason="Empty input")
        text = text.strip()
        with self._lifecycle:
            if self._closing:
                return interaction.SubmissionResult("closed", reason="Runtime is closing")
            if not self._started:
                return interaction.SubmissionResult("invalid", reason="Runtime is not started")
            if self._busy:
                return interaction.SubmissionResult("busy", reason="A turn or command is running")
            kind = "command" if find_command(text.split()[0]) else "turn"
            submission_id = uuid.uuid4().hex
            self._busy = True
            self._pending = (submission_id, kind, text)
            self._background_paused = False
            self._wake.set()
            return interaction.SubmissionResult("accepted", submission_id, kind)

    def request_stop(self, turn_id: str) -> bool:
        with self._lifecycle:
            if self._turn != turn_id:
                return False
            self._stop.set()
            self._background_paused = True
            self.channel.cancel_turn(turn_id)
            return True

    def close(self):
        with self._lifecycle:
            if self._closing:
                return
            self._closing = True
            if self._stop is not None:
                self._stop.set()
                self.channel.cancel_turn(self._turn)
            self.channel.close_approvals()
            self._wake.set()

    def _snapshot(self, context):
        # All calls run on the execution thread while agent_lock is held.
        todos = self.lead_context.todos
        session_snapshot = self.session.snapshot or {}
        context.publish("state_snapshot", history=json_value(self.session.history),
                        goal=host.GOAL.snapshot(), todos=todos,
                        subagents=subagent_runtime.list_subagents(), model=core.MODEL,
                        busy=self._busy, closing=self._closing,
                        lead_main_model_usage=dict(self._usage),
                        workdir=session_snapshot.get("workdir"),
                        task_id=session_snapshot.get("task_id"),
                        worktree=session_snapshot.get("worktree"),
                        active_user_request=session_snapshot.get("active_user_request"))

    def _background_ready(self):
        if self.session.session_id is None:
            return False
        with scheduler.cron_lock:
            fired = bool(scheduler.cron_queue)
        return fired or subagent_runtime.BUS.peek("lead") or scheduler.has_pending_background()

    def _record_usage(self, usage):
        for key in ("input_tokens", "output_tokens"):
            self._usage[key] += usage.get(key, 0) or 0

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
                    self._usage["model_calls"] += 1
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
                        pending, self._pending = self._pending, None
                        if pending is not None:
                            submission_id, kind, text = pending
                            context = self._context()
                            if kind == "command":
                                context.publish("command_result", submission_id=submission_id,
                                                command=text.split()[0], status="failed",
                                                data={"error": "Runtime is closing"})
                            else:
                                context = replace(context, turn_id=uuid.uuid4().hex)
                                context.publish("turn_started", submission_id=submission_id)
                                self._complete_event(context, "turn_finished", submission_id=submission_id,
                                                outcome=asdict(interaction.TurnOutcome("stopped", "Runtime is closing")),
                                                summary={"elapsed_seconds": 0, "tool_calls": 0,
                                                         "approvals": 0, "lead_usage":
                                                         {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}})
                        break
                    work, self._pending = self._pending, None
                    if work is None and not self._busy and not self._background_paused:
                        if background_ready:
                            self._busy = True
                            work = (None, "background", self.session.active_request)
                if work is not None:
                    self._execute(*work)
                else:
                    with host.agent_lock, interaction.execution_context(self._context()):
                        self._checkpoint(poll=True)
        except Exception as exc:
            self._context().publish("runtime_error", error=f"{type(exc).__name__}: {exc}")
            self.close()
        finally:
            self._shutdown()

    def _execute(self, submission_id, kind, text):
        with host.agent_lock:
            context = self._context(origin="frontend" if kind != "background" else "background")
            try:
                if kind == "command":
                    with interaction.execution_context(context):
                        followup = self._command(text)
                    if followup is None:
                        with self._lifecycle:
                            self._busy = False
                        self._snapshot(self._context())
                        context = self._context()
                        self._complete_event(context, "command_result", submission_id=submission_id,
                                             command=text.split()[0], status="completed", data=self._command_data)
                        return
                    text = followup
                with self._lifecycle:
                    self._turn = uuid.uuid4().hex
                    self._turn_started_at = time.monotonic()
                    self._turn_usage = dict(self._usage)
                    self._stop = threading.Event()
                    if self._closing:
                        self._stop.set()
                    context = self._context(turn_id=self._turn,
                                            origin="background" if kind == "background" else "frontend",
                                            stop_event=self._stop)
                with interaction.execution_context(context):
                    self.session.finish_pending_tools()
                    if kind == "background":
                        inbox = subagent_runtime.consume_lead_inbox(route_protocol=True)
                        if inbox:
                            self.session.history.append({"role": "user", "content": subagent_runtime.format_subagent_events(inbox)})
                        with scheduler.cron_lock:
                            fired = list(scheduler.cron_queue)
                        if fired:
                            text = "\n".join(f"Run scheduled task: {job.prompt}" for job in fired)
                        self._record("background_notification", text, save=False)
                    else:
                        self.session.history.append({"role": "user", "content": text})
                        self._record("user_input", text, save=False)
                    # The first user message may have created the durable session ID.
                    context = replace(context, session_id=self.session.session_id)
                with interaction.execution_context(context):
                    if kind == "command":
                        context.publish("command_result", submission_id=submission_id,
                                        command="/goal", status="completed", data=self._command_data,
                                        turn_id=context.turn_id, recorded=True)
                    if kind != "background":
                        context.publish("user_message", text=text, submission_id=submission_id)
                        hooks.trigger_hooks("UserPromptSubmit", text)
                    context.publish("turn_started", submission_id=submission_id)
                    self._checkpoint()
                    self._snapshot(context)
                    try:
                        outcome = host.agent_loop(self.session.history, text, self._checkpoint, self._record_usage)
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
                    # A command may already have activated a different session.
                    context = self._context()
                self._snapshot(context)
                self._complete_event(context, "command_result" if kind == "command" else "turn_finished",
                                submission_id=submission_id, command=text.split()[0],
                                status="failed", data={"error": str(exc)},
                                outcome=asdict(interaction.TurnOutcome("failed", str(exc))),
                                summary=self._turn_summary(context))

    def _command(self, text):
        parts = text.split()
        command = parts[0]
        self._command_data = {}
        if command == "/quit":
            self.close()
        elif command == "/goal":
            argument = text[5:].strip()
            if not argument:
                self._command_data = {"text": host.GOAL.status(0)}
                self._record("goal_query", text, save=False)
            elif argument.lower() in goal.CLEAR_ALIASES:
                self._command_data = {"text": host.GOAL.clear()}
                self._record("goal_clear", text, save=False)
            else:
                host.GOAL.set_goal(argument)
                self._record("goal_set", argument, save=False)
                self._command_data = {"text": f"Goal set: {argument}"}
                return argument
        elif command == "/resume" and len(parts) == 1:
            entries, errors = self.session.store.list_sessions()
            self._command_data = {"sessions": entries, "errors": errors}
        else:
            if len(parts) != 2:
                raise SessionError(f"Usage: {command} <session_id>")
            target = parts[1]
            self.session.store.path(target)
            if command == "/delete":
                if target == self.session.session_id:
                    raise SessionError("Cannot delete the active session")
                self.session.store.delete(target)
                self._command_data = {"text": f"Deleted {target}"}
            elif target == self.session.session_id:
                self._command_data = {"text": f"Already using {target}"}
            else:
                self._command_data = self.session_switch.switch(target)
        return None

    # -- 会话切换已移入 session_switch.SessionSwitch；这里只提供它的窄回调。 --

    def _read_goal(self):
        """返回运行时当前持有的 goal 控制器。"""
        return host.GOAL

    def _replace_goal(self, value):
        """替换运行时持有的 goal 控制器。"""
        host.GOAL = value

    def _pause_background(self):
        """恢复后处理失败时暂停后台输入，避免重复消费同一批损坏数据。"""
        with self._lifecycle:
            self._background_paused = True

    def _after_activate(self):
        """切换落地后刷新运行时自身的用量与代际。"""
        self._usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
        self._generation += 1


    def _turn_summary(self, context, outcome=None):
        records = self.session.display_records()
        tool_calls = sum(1 for record in records
                         if record["type"] == "tool_call" and record["turn_id"] == context.turn_id)
        approvals = sum(1 for record in records
                        if record["type"] == "approval_requested" and record["turn_id"] == context.turn_id)
        return {"elapsed_seconds": round(time.monotonic() - self._turn_started_at, 3)
                if self._turn_started_at is not None else 0,
                "tool_calls": tool_calls, "approvals": approvals,
                "lead_usage": {key: self._usage[key] - self._turn_usage[key] for key in self._usage}}

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
                self._context().publish("runtime_error", error=f"Could not save background results: {exc}")
            saved = self._checkpoint() and collected
            self._context().publish("runtime_closed", saved=saved)
        self.channel.close()
