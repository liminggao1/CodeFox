"""Local interactive sessions: one atomic JSON snapshot per conversation."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .goal import GoalController, GoalError


VERSION = 1
SESSION_ID = re.compile(r"\d{8}-\d{6}-[a-f0-9]{8}")


class SessionError(Exception):
    """A session file or command could not be used."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self.directory = workdir / ".transcripts" / "sessions"

    def path(self, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise SessionError("Invalid session ID; use the full ID from /resume")
        return self.directory / f"{session_id}.json"

    def create(self) -> dict:
        session_id = (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-")
                      + uuid.uuid4().hex[:8])
        return {"version": VERSION, "id": session_id,
                "created_at": utc_now(), "updated_at": utc_now(),
                "messages": [], "events": [], "goal": None,
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

    def load(self, session_id: str) -> dict:
        path = self.path(session_id)
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict) or snapshot.get("version") != VERSION:
                raise SessionError("Unsupported session version")
            if snapshot.get("id") != session_id:
                raise SessionError("Session ID does not match its filename")
            for key in ("created_at", "updated_at"):
                datetime.fromisoformat(snapshot[key])
            if not isinstance(snapshot["active_user_request"], str):
                raise SessionError("Invalid active user request")
            messages, events = snapshot["messages"], snapshot["events"]
            if (not isinstance(messages, list) or not isinstance(events, list)
                    or not all(isinstance(event, dict) for event in events)):
                raise SessionError("Invalid session messages or events")
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
            # Validate goal state before any active context can be replaced.
            GoalController.from_snapshot(None, snapshot["goal"])
            return snapshot
        except SessionError:
            raise
        except (OSError, ValueError, TypeError, KeyError, GoalError) as exc:
            raise SessionError(f"Cannot read session {session_id}: {exc}") from exc

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
                                 "message_count": len(snapshot["messages"])})
            except SessionError as exc:
                errors.append(f"{path.name}: {exc}")
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return sessions[:limit], errors

    def delete(self, session_id: str) -> None:
        try:
            self.path(session_id).unlink()
        except OSError as exc:
            raise SessionError(f"Cannot delete session {session_id}: {exc}") from exc


class InteractiveSession:
    """Own the active snapshot; host mutations share a single save callback.

    The CLI and background event loop call this object under agent_lock.
    Failed writes retain memory and retry at the next checkpoint.
    """

    def __init__(self, store: SessionStore, history: list, goal_getter, report):
        self.store = store
        self.history = history
        self.goal_getter = goal_getter
        self.report = report
        self.snapshot: dict | None = None
        self.dirty = False
        self.active_request = "(no active user request)"

    @property
    def session_id(self) -> str | None:
        return self.snapshot["id"] if self.snapshot else None

    def record(self, kind: str, text: str = "", result: str = "ok") -> bool:
        if self.snapshot is None:
            self.snapshot = self.store.create()
            self.report(f"[session] {self.session_id}")
        if kind in {"user_input", "goal_set"} and result == "ok":
            self.active_request = text
        self.snapshot["events"].append({"id": uuid.uuid4().hex, "at": utc_now(),
                                        "type": kind, "text": text[:200],
                                        "result": result,
                                        "message_count": len(self.history)})
        return self.save()

    def save(self) -> bool:
        if self.snapshot is None:
            return True
        self.dirty = True
        self.snapshot.update(messages=self.history,
                             goal=self.goal_getter().snapshot(), updated_at=utc_now(),
                             active_user_request=self.active_request)
        try:
            self.store.save(self.snapshot)
        except (OSError, ValueError, TypeError, SessionError) as exc:
            self.report(f"[session error] Could not save {self.session_id}: {exc}")
            return False
        self.dirty = False
        return True

    def activate(self, snapshot: dict) -> None:
        self.history[:] = snapshot["messages"]
        self.snapshot = snapshot
        self.active_request = snapshot["active_user_request"]
        self.record("resume", snapshot["id"])

    def finish_pending_tools(self) -> None:
        """Supply interrupted tool results without rerunning side effects."""
        if not self.history:
            return
        last = self.history[-1]
        if last.get("role") != "assistant" or not isinstance(last.get("content"), list):
            return
        content = json_value(last["content"])
        results = [{"type": "tool_result", "tool_use_id": block["id"],
                    "content": "Interrupted before the tool result was saved; verify current state before retrying.",
                    "is_error": True}
                   for block in content if block.get("type") == "tool_use"]
        if results:
            self.history.append({"role": "user", "content": results})
            self.save()
