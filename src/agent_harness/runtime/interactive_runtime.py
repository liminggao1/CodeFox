"""One serialized interactive driver shared by CLI and Textual adapters."""

from __future__ import annotations

from dataclasses import asdict, replace
import os
import threading
import time
import uuid
from pathlib import Path

from .. import core
from ..agents import goal
from .. import host
from . import interaction
from .local_commands import find_command
from .session_store import InteractiveSession, SessionError, SessionStore, json_value


class InteractiveRuntime:
    def __init__(self, *, channel=None, store=None):
        self.channel = channel or interaction.InteractionChannel()
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

    def _session_context(self):
        with host.tasks.task_lock:
            host.tasks.lead_session_id = self.session.session_id
            return host.tasks.lead_context()

    def _context(self, **kwargs):
        return interaction.ExecutionContext(self.channel, self.session.session_id,
                                              self._generation, checkpoint=self._checkpoint,
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
        with host.tasks.task_lock:
            todos = json_value(host.tasks.CURRENT_TODOS)
        context.publish("state_snapshot", history=json_value(self.session.history),
                        goal=host.GOAL.snapshot(), todos=todos,
                        subagents=host.subagent.list_subagents(), model=core.MODEL,
                        busy=self._busy, closing=self._closing,
                        lead_main_model_usage=dict(self._usage))

    def _background_ready(self):
        if self.session.session_id is None:
            return False
        with host.scheduler.cron_lock:
            fired = bool(host.scheduler.cron_queue)
        return fired or host.subagent.BUS.peek("lead") or host.scheduler.has_pending_background()

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
                host.tasks.activate_lead_context(None, {"worktree": None, "todos": []})
                host.tasks.save_lead_context = self._checkpoint
                host.start_runtime_services()
                host.subagent.consume_lead_inbox(route_protocol=False)
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
                    if kind == "background":
                        inbox = host.subagent.consume_lead_inbox(route_protocol=True)
                        if inbox:
                            self.session.history.append({"role": "user", "content": host.subagent.format_subagent_events(inbox)})
                        with host.scheduler.cron_lock:
                            fired = list(host.scheduler.cron_queue)
                        if fired:
                            text = "\n".join(f"Run scheduled task: {job.prompt}" for job in fired)
                        self.session.record("background_notification", text, save=False)
                    else:
                        self.session.finish_pending_tools()
                        self.session.history.append({"role": "user", "content": text})
                        self.session.record("user_input", text, save=False)
                    # The first user message may have created the durable session ID.
                    context = replace(context, session_id=self.session.session_id)
                with interaction.execution_context(context):
                    if kind == "command":
                        context.publish("command_result", submission_id=submission_id,
                                        command="/goal", status="completed", data=self._command_data,
                                        turn_id=context.turn_id, recorded=True)
                    if kind != "background":
                        context.publish("user_message", text=text, submission_id=submission_id)
                        host.trigger_hooks("UserPromptSubmit", text)
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
                    self.session.record("turn_complete", text, outcome.status, save=False)
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
                self.session.record("goal_query", text, save=False)
            elif argument.lower() in goal.CLEAR_ALIASES:
                self._command_data = {"text": host.GOAL.clear()}
                self.session.record("goal_clear", text, save=False)
            else:
                host.GOAL.set_goal(argument)
                self.session.record("goal_set", argument, save=False)
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
                self._command_data = self._resume(target)
        return None

    # -- Session switching: preparation is read-only; activation owns all state changes. --

    def _prepare_resume(self, target):
        snapshot = self.session.store.load(target)
        restored_goal = goal.GoalController.from_snapshot(
            host.GOAL.evaluator, snapshot["goal"], host.GOAL.block_cap)
        root = Path(snapshot["workdir"]).resolve()
        if not root.is_dir():
            raise SessionError(f"Session startup directory does not exist: {root}")
        if (host.scheduler.has_background_tasks() or host.subagent.list_subagents()
                or host.subagent.BUS.peek("lead")):
            raise SessionError("Background work or results remain; finish them and retry /resume")
        with host.scheduler.cron_lock:
            if host.scheduler.cron_queue:
                raise SessionError("Scheduled results remain; finish them and retry /resume")
        assignment = host.tasks.validate_lead_context(target, snapshot, workdir=root)
        return snapshot, root, restored_goal, assignment

    def _resume(self, target):
        snapshot, root, restored_goal, assignment = self._prepare_resume(target)
        if not self._checkpoint():
            raise SessionError("Current session could not be saved; context was not switched")
        directory_changed = root != core.WORKDIR.resolve()
        # Do not join cron while holding its lock. Recheck after it has stopped.
        if directory_changed:
            host.scheduler.stop_runtime_services()
        try:
            with host.scheduler.cron_lock:
                if host.scheduler.cron_queue:
                    raise SessionError("Scheduled results remain; finish them and retry /resume")
                if directory_changed:
                    os.chdir(root)
                self._activate_resume(snapshot, root, restored_goal, assignment)
        except Exception:
            if directory_changed and core.WORKDIR.resolve() != root:
                host.scheduler.start_runtime_services(load_jobs=False)
            raise
        with interaction.execution_context(self._context()):
            try:
                self.session.record("resume", target, save=False)
                saved = self._checkpoint()
                if directory_changed:
                    host.scheduler.clear_runtime_jobs()
                    try:
                        host.subagent.consume_lead_inbox(route_protocol=False)
                    finally:
                        host.scheduler.start_runtime_services()
            except Exception as exc:
                # Do not repeatedly consume the same damaged background input.
                with self._lifecycle:
                    self._background_paused = True
                raise SessionError(f"Resumed {target} at {root}; post-resume processing failed: {exc}") from exc
            finally:
                self._context().publish("session_changed",
                                        records=self.session.display_records(historical=True))
        text = f"Resumed {target} at {root}"
        if not saved:
            text += "; context could not be saved and will be retried on the next interaction"
        return {"text": text, "workdir": str(root), "saved": saved}

    def _activate_resume(self, snapshot, root, restored_goal, assignment):
        core.WORKDIR = root
        self.session.store.workdir = root
        host.mcp_clients.clear()
        host.rounds_since_todo = 0
        host.GOAL = restored_goal
        host.tasks.activate_lead_context(snapshot["id"], snapshot, assignment)
        self.session.activate(snapshot)
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
            host.scheduler.stop_runtime_services()
            for child in host.subagent.list_subagents():
                if child["mode"] == "persistent":
                    host.subagent.run_request_shutdown(child["name"])
            core.terminal_print("Closing background execution")
        host.file_tools._stop_all_shell_processes()
        host.subagent.wait_for_subagents()
        host.scheduler.wait_for_background_tasks()
        with host.agent_lock, interaction.execution_context(self._context()):
            saved = self._checkpoint()
            host.tasks.save_lead_context = None
            self._context().publish("runtime_closed", saved=saved)
        self.channel.close()
