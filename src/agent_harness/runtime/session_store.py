"""Local interactive sessions: one atomic JSON snapshot per conversation."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..agents.goal import GoalController, GoalError
from ..context.prompt_state import PromptState, is_context_message, validate_snapshot
from . import interaction


SESSION_ID = re.compile(r"\d{8}-\d{6}-[a-f0-9]{8}")


class SessionError(Exception):
    """A session file or command could not be used."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sessions_dir() -> Path:
    return Path.home() / ".agent-harness" / "sessions"


def json_value(value):
    """Preserve SDK content blocks as dictionaries instead of string reprs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return json_value(value.model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return json_value(vars(value))
    raise TypeError(f"Unsupported session content: {type(value).__name__}")


class SessionStore:
    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()
        self.directory = sessions_dir()

    def path(self, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise SessionError("Invalid session ID; use the full ID from /resume")
        return self.directory / f"{session_id}.json"

    def create(self) -> dict:
        session_id = (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-")
                      + uuid.uuid4().hex[:8])
        return {"id": session_id,
                "created_at": utc_now(), "updated_at": utc_now(),
                "messages": [], "events": [], "goal": None,
                "workdir": str(self.workdir), "task_id": None,
                "worktree": None, "todos": [],
                "active_user_request": "(no active user request)"}

    def save(self, snapshot: dict) -> None:
        path = self.path(snapshot["id"])
        payload = json.dumps(json_value(snapshot), ensure_ascii=False,
                             indent=2, allow_nan=False) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=self.directory, suffix=".tmp",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def diagnostics_path(self, session_id: str) -> Path:
        return self.path(session_id).with_suffix(".diagnostics.jsonl")

    def artifacts_path(self, session_id: str) -> Path:
        return self.directory / f"{self.path(session_id).stem}.artifacts"

    def load(self, session_id: str) -> dict:
        path = self.path(session_id)
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict):
                raise SessionError("Invalid session context")
            if snapshot.get("id") != session_id:
                raise SessionError("Session ID does not match its filename")
            for key in ("created_at", "updated_at"):
                datetime.fromisoformat(snapshot[key])
            validate_snapshot(snapshot.get("prompt_state"))
            if not isinstance(snapshot["active_user_request"], str):
                raise SessionError("Invalid active user request")
            if not isinstance(snapshot["workdir"], str) or not Path(snapshot["workdir"]).is_absolute():
                raise SessionError("Invalid startup directory")
            from ..workspace import tasks, worktrees
            if snapshot["task_id"] is not None and (
                    not isinstance(snapshot["task_id"], str)
                    or not tasks.TASK_ID_PATTERN.fullmatch(snapshot["task_id"])):
                raise SessionError("Invalid task ID")
            if snapshot["worktree"] is not None and worktrees.validate_worktree_name(snapshot["worktree"]):
                raise SessionError("Invalid worktree name")
            todos = snapshot["todos"]
            if not isinstance(todos, list) or not all(
                    isinstance(todo, dict) and isinstance(todo.get("content"), str)
                    and todo.get("status") in {"pending", "in_progress", "completed"}
                    for todo in todos):
                raise SessionError("Invalid TODO context")
            messages, events = snapshot["messages"], snapshot["events"]
            if (not isinstance(messages, list) or not isinstance(events, list)
                    or not all(isinstance(event, dict) for event in events)):
                raise SessionError("Invalid session messages or events")
            seen = set()
            for event in events:
                if (not isinstance(event.get("id"), str) or not event["id"]
                        or event["id"] in seen or not isinstance(event.get("type"), str)):
                    raise SessionError("Invalid or duplicate event ID/type")
                seen.add(event["id"])
                datetime.fromisoformat(event["at"])
                if event.get("kind") == "display":
                    self._validate_record(event)
                elif event.get("kind") != "summary":
                    raise SessionError("Invalid event kind; expected display or summary")
                if event.get("kind") == "summary" and event["type"] == "model_usage":
                    if type(event.get("input_tokens")) is not int or event["input_tokens"] < 0:
                        raise SessionError("Invalid model input token measurement")
            if messages and not any(event.get("kind") == "display" and
                                    event["type"] in {"user_message", "assistant_message", "tool_call"}
                                    for event in events):
                raise SessionError("Messages are missing corresponding display records")
            for message in messages:
                if (not isinstance(message, dict)
                        or message.get("role") not in {"user", "assistant"}
                        or not isinstance(message.get("content"), (str, list))):
                    raise SessionError("Invalid session message")
                content = message["content"]
                if isinstance(content, list) and not all(
                        isinstance(block, dict) and isinstance(block.get("type"), str)
                        for block in content):
                    raise SessionError("Invalid session content block")
                for block in content if isinstance(content, list) else []:
                    kind = block["type"]
                    required = {"text": ("text",), "tool_use": ("id", "name"),
                                "tool_result": ("tool_use_id",)}.get(kind, ())
                    if not all(isinstance(block.get(key), str) for key in required):
                        raise SessionError(f"Invalid {kind} fields")
                    if kind == "tool_use" and not isinstance(block.get("input"), dict):
                        raise SessionError("Invalid tool input")
                    if message["role"] == "assistant" and kind in {"text", "tool_use"}:
                        display_type = "assistant_message" if kind == "text" else "tool_call"
                        field = "text" if kind == "text" else "id"
                        if not any(event.get("kind") == "display" and event["type"] == display_type
                                   and event["source"] == "lead"
                                   and event["payload"].get(field) == block[field] for event in events):
                            raise SessionError(f"Missing corresponding {display_type} record")
            # Validate goal state before any active context can be replaced.
            GoalController.from_snapshot(None, snapshot["goal"])
            return snapshot
        except SessionError:
            raise
        except (OSError, ValueError, TypeError, KeyError, GoalError) as exc:
            raise SessionError(f"Cannot read session {session_id}: {exc}") from exc

    @staticmethod
    def _validate_record(record):
        kind, payload = record["type"], record.get("payload")
        if (kind not in interaction.DISPLAY_FACT_TYPES or not isinstance(payload, dict)
                or not isinstance(record.get("source"), str)
                or not record["source"] or "turn_id" not in record
                or record["turn_id"] is not None and not isinstance(record["turn_id"], str)):
            raise SessionError("Invalid display record fields")
        required = {
            "user_message": {"text": str}, "assistant_message": {"text": str},
            "tool_call": {"id": str, "name": str, "input": dict},
            "tool_result": {"tool_use_id": str}, "todo_updated": {"todos": list},
            "subagent_status": {"name": str, "mode": str, "status": str},
            "background_status": {"task_id": str, "status": str},
            "approval_requested": {"request_id": str, "tool_name": str,
                                   "tool_call_id": str, "arguments": dict},
            "approval_decided": {"request_id": str, "tool_name": str,
                                 "tool_call_id": str, "decision": str},
            "command_result": {"command": str, "status": str, "data": dict},
            "turn_finished": {"outcome": dict, "summary": dict},
            "execution_status": {"operation_id": str, "phase": str, "name": str, "state": str},
            "system_error": {"error": str},
        }[kind]
        if not all(isinstance(payload.get(key), expected) for key, expected in required.items()):
            raise SessionError(f"Invalid {kind} display payload")
        if kind == "execution_status" and (payload["phase"] not in {"model", "tool", "hook", "goal", "memory"}
                or payload["state"] not in {"running", "postprocessing", "completed", "failed", "denied", "stopped", "discarded", "truncated"}
                or not payload["operation_id"]):
            raise SessionError("Invalid execution lifecycle")
        if "model_call_id" in payload and (not isinstance(payload["model_call_id"], str) or not payload["model_call_id"]):
            raise SessionError("Invalid model call identity")
        if kind == "tool_result" and ("content" not in payload or payload.get("status") not in
                {"returned", "success", "failed", "denied", "stopped", "started"}):
            raise SessionError("Invalid tool result display state")
        if kind == "approval_decided" and payload["decision"] not in {"approve", "deny", "closed"}:
            raise SessionError("Invalid approval decision")
        if kind == "todo_updated" and not all(
                isinstance(todo, dict) and isinstance(todo.get("content"), str)
                and todo.get("status") in {"pending", "in_progress", "completed"}
                for todo in payload["todos"]):
            raise SessionError("Invalid TODO display context")
        if kind == "turn_finished":
            outcome, summary = payload["outcome"], payload["summary"]
            if (outcome.get("status") not in {"completed", "failed", "stopped", "truncated"}
                    or not isinstance(outcome.get("reason"), str)
                    or not isinstance(summary.get("elapsed_seconds"), (int, float))
                    or summary["elapsed_seconds"] < 0
                    or not all(isinstance(summary.get(key), int) and summary[key] >= 0
                               for key in ("tool_calls", "approvals"))
                    or not isinstance(summary.get("lead_usage"), dict)
                    or not all(isinstance(summary["lead_usage"].get(key), int)
                               and summary["lead_usage"][key] >= 0
                               for key in ("model_calls", "input_tokens", "output_tokens"))):
                raise SessionError("Invalid turn summary")

    def list_sessions(self, limit: int = 20) -> tuple[list[dict], list[str]]:
        sessions, errors = [], []
        for path in self.directory.glob("*.json"):
            try:
                snapshot = self.load(path.stem)
                first = next((event.get("text", "") for event in snapshot["events"]
                              if event.get("type") in {"user_input", "goal_query",
                                                       "goal_set", "goal_clear"}), "")
                sessions.append({"id": snapshot["id"],
                                 "updated_at": snapshot["updated_at"],
                                 "summary": str(first).replace("\n", " ")[:80],
                                 "message_count": sum(not is_context_message(message)
                                                      for message in snapshot["messages"])})
            except SessionError as exc:
                # 读不出来的文件也列进会话列表，让用户能直接删除它。
                # 字段只是展示占位，不代表文件内容有效。
                errors.append(f"{path.name}: {exc}")
                sessions.append({"id": path.stem, "updated_at": "", "summary": str(exc),
                                 "message_count": 0, "invalid": True})
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return sessions[:limit], errors

    def delete(self, session_id: str) -> None:
        try:
            self.diagnostics_path(session_id).unlink(missing_ok=True)
            shutil.rmtree(self.artifacts_path(session_id), ignore_errors=False)
            self.path(session_id).unlink()
        except FileNotFoundError as exc:
            if exc.filename and Path(exc.filename) == self.artifacts_path(session_id):
                self.path(session_id).unlink()
            else:
                raise SessionError(f"Cannot delete session {session_id}: {exc}") from exc
        except OSError as exc:
            raise SessionError(f"Cannot delete session {session_id}: {exc}") from exc


class InteractiveSession:
    """Own the active snapshot; host mutations share a single save callback.

    The shared interactive driver calls this object under agent_lock.
    Failed writes retain memory and retry at the next checkpoint.
    """

    def __init__(self, store: SessionStore, history: list, goal_getter, report,
                 context_getter=None):
        self.store = store
        self.history = history
        self.goal_getter = goal_getter
        self.report = report
        self.context_getter = context_getter
        self.snapshot: dict | None = None
        self.dirty = False
        self.active_request = "(no active user request)"
        self.prompt_state = PromptState()

    @property
    def session_id(self) -> str | None:
        return self.snapshot["id"] if self.snapshot else None

    def record(self, kind: str, text: str = "", result: str = "ok", *, save=True) -> bool:
        if self.snapshot is None:
            self.snapshot = self.store.create()
            self.report(f"[session] {self.session_id}")
        if kind in {"user_input", "goal_set"} and result == "ok":
            self.active_request = text
        self.snapshot["events"].append({"id": uuid.uuid4().hex, "at": utc_now(),
                                        "kind": "summary", "type": kind, "text": text[:200],
                                        "result": result,
                                        "message_count": sum(not is_context_message(message)
                                                             for message in self.history)})
        self.dirty = True
        return self.save() if save else True

    def append_record(self, record: dict) -> bool:
        if self.snapshot is None:
            return False
        if any(event["id"] == record["id"] for event in self.snapshot["events"]):
            return False
        self.snapshot["events"].append(json_value(record))
        self.dirty = True
        return True

    def record_input_tokens(self, value: int) -> None:
        """Persist each API input measurement at the next normal checkpoint."""
        self.append_record({"id": uuid.uuid4().hex, "at": utc_now(),
                            "kind": "summary", "type": "model_usage", "input_tokens": value})

    def last_input_tokens(self) -> int | None:
        """Session measurement, separate from cumulative per-turn usage."""
        if self.snapshot is not None:
            for event in reversed(self.snapshot["events"]):
                if event.get("kind") == "summary" and event["type"] == "model_usage":
                    return event["input_tokens"]
        # A populated history without a measurement is unknown, not zero usage.
        return None if self.history else 0

    def display_records(self, *, historical=False) -> list[dict]:
        records = json_value([event for event in self.snapshot["events"]
                              if event.get("kind") == "display"]) if self.snapshot else []
        if historical:
            decided = {r["payload"]["request_id"] for r in records if r["type"] == "approval_decided"}
            results = {(r["turn_id"], r["source"], r["payload"]["tool_use_id"])
                       for r in records if r["type"] == "tool_result"}
            terminal_operations = set()
            terminal_tasks = set()
            terminal_background = set()
            for record in records:
                payload = record["payload"]
                if record["type"] == "execution_status" and payload.get("state") in {
                        "completed", "failed", "stopped", "discarded", "truncated"}:
                    terminal_operations.add((record["turn_id"], record["source"],
                                             payload.get("model_call_id"), payload.get("operation_id")))
                elif record["type"] == "subagent_status" and payload.get("status") in {
                        "completed", "failed", "stopped", "denied", "idle", "closed"}:
                    terminal_tasks.add((record["turn_id"], record["source"],
                                        payload.get("name"), payload.get("task_id")))
                elif record["type"] == "background_status" and payload.get("status") in {
                        "completed", "failed", "stopped", "denied"}:
                    terminal_background.add((record["turn_id"], record["source"], payload.get("task_id")))
            for record in records:
                payload = record["payload"]
                payload["historical"] = True
                if record["type"] == "approval_requested" and payload["request_id"] not in decided:
                    payload["interrupted"] = True
                if record["type"] == "tool_call" and (record["turn_id"], record["source"], payload["id"]) not in results:
                    payload["result_missing"] = True
                if record["type"] == "execution_status" and payload.get("state") in {"running", "working", "pending", "postprocessing"}:
                    key = (record["turn_id"], record["source"], payload.get("model_call_id"), payload.get("operation_id"))
                    if key not in terminal_operations:
                        payload.update(state="interrupted", interrupted=True,
                                       reason="上次中断，结束状态未记录")
                if record["type"] == "subagent_status" and payload.get("status") in {
                        "running", "working", "pending", "waiting_approval", "stopping"}:
                    key = (record["turn_id"], record["source"], payload.get("name"), payload.get("task_id"))
                    if key not in terminal_tasks:
                        payload.update(status="interrupted", interrupted=True)
                if record["type"] == "background_status" and payload.get("status") in {"running", "working", "pending"}:
                    key = (record["turn_id"], record["source"], payload.get("task_id"))
                    if key not in terminal_background:
                        payload.update(status="interrupted", interrupted=True)
        return records

    def save(self) -> bool:
        if self.snapshot is None:
            return True
        self.dirty = True
        try:
            self.snapshot.update(messages=self.history,
                                 goal=self.goal_getter().snapshot(), updated_at=utc_now(),
                                 active_user_request=self.active_request,
                                 prompt_state=self.prompt_state.snapshot())
            if self.context_getter is not None:
                self.snapshot.update(self.context_getter())
            self.store.save(self.snapshot)
        except (OSError, ValueError, TypeError, SessionError) as exc:
            error = f"Could not save {self.session_id}: {exc}"
            if interaction.current_context() is None:
                self.report(f"[session error] {error}")
            else:
                interaction.publish("session_save_error", session_id=self.session_id, error=error)
            return False
        self.dirty = False
        interaction.publish("session_saved", session_id=self.session_id)
        return True

    def activate(self, snapshot: dict) -> None:
        """Replace memory; the caller records and saves after activation completes."""
        self.history[:] = snapshot["messages"]
        self.snapshot = snapshot
        self.dirty = False
        self.active_request = snapshot["active_user_request"]
        self.prompt_state = PromptState(snapshot.get("prompt_state"))

    def finish_pending_tools(self) -> None:
        """Supply interrupted tool results without rerunning side effects."""
        if not self.history:
            return
        last = self.history[-1]
        if last.get("role") != "assistant" or not isinstance(last.get("content"), list):
            return
        content = json_value(last["content"])
        records = self.display_records()
        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            call = next((r for r in reversed(records) if r["type"] == "tool_call" and r["source"] == "lead"
                         and r["payload"]["id"] == block["id"]), None)
            saved = next((r for r in reversed(records) if call is not None and r["type"] == "tool_result"
                          and r["source"] == "lead" and r["turn_id"] == call["turn_id"]
                          and r["payload"]["tool_use_id"] == block["id"]
                          and r["payload"].get("model_call_id") == call["payload"].get("model_call_id")), None)
            if saved is not None:
                data = saved["payload"]
                result = {"type": "tool_result", "tool_use_id": block["id"], "content": data["content"]}
                if "is_error" in data:
                    result["is_error"] = data["is_error"]
            else:
                result = {"type": "tool_result", "tool_use_id": block["id"],
                          "content": "Interrupted before the tool result was saved; verify current state before retrying.",
                          "is_error": True}
            results.append(result)
        if results:
            self.history.append({"role": "user", "content": results})
            self.dirty = True
