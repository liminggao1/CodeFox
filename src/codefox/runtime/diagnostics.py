"""Session-bound diagnostics, stored separately from model and display history."""

from __future__ import annotations

from datetime import datetime
import json
import threading


class DiagnosticError(Exception):
    """A diagnostic file cannot be read or written; conversation remains usable."""


def log_record(event):
    return {"id": event.id, "at": event.at, "session_id": event.session_id,
            "turn_id": event.turn_id, "source": event.source,
            "origin": event.payload.get("origin", "background"),
            "text": event.payload["text"]}


class DiagnosticStore:
    def __init__(self, path):
        self._path = path
        self._lock = threading.RLock()
        self._records = {}
        self._ids = {}
        self._pending = {}
        self._blocked = {}
        self._reported = {}

    def _load(self, session_id):
        if session_id in self._records:
            return
        path = self._path(session_id)
        records, seen = [], set()
        try:
            if path.exists():
                with path.open(encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        try:
                            record = json.loads(line)
                            if (not isinstance(record, dict)
                                    or not all(isinstance(record.get(key), str) for key in
                                               ("id", "at", "session_id", "source", "origin", "text"))
                                    or not record["id"] or record["id"] in seen
                                    or record["session_id"] != session_id
                                    or record.get("turn_id") is not None
                                    and not isinstance(record["turn_id"], str)
                                    or "turn_id" not in record):
                                raise ValueError("Invalid diagnostic record")
                            datetime.fromisoformat(record["at"])
                        except (ValueError, TypeError) as exc:
                            raise DiagnosticError(f"Cannot read diagnostics {path}:{number}: {exc}") from exc
                        records.append(record)
                        seen.add(record["id"])
        except (OSError, UnicodeError, DiagnosticError) as exc:
            error = str(exc) if isinstance(exc, DiagnosticError) else f"Cannot read diagnostics {path}: {exc}"
            self._blocked[session_id] = error
            # Preserve the original file; new live logs are still retained in memory.
            self._records[session_id], self._ids[session_id] = [], set()
            raise DiagnosticError(error) from exc
        self._records[session_id], self._ids[session_id] = records, seen

    def read(self, session_id):
        with self._lock:
            try:
                self._load(session_id)
            except DiagnosticError as exc:
                self._reported[session_id] = str(exc)
                raise
            return [dict(record) for record in self._records[session_id]]

    def _append(self, session_id, records):
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
                          for record in records).encode("utf-8")
        with path.open("a+b") as handle:
            handle.seek(0, 2)
            start = handle.tell()
            try:
                if handle.write(payload) != len(payload):
                    raise OSError("Incomplete diagnostic write")
                handle.flush()
            except OSError:
                # Roll back a partial append before retrying the same record IDs.
                try:
                    handle.seek(start)
                    handle.truncate()
                    handle.flush()
                except OSError as exc:
                    self._blocked[session_id] = f"Cannot roll back diagnostics {path}: {exc}"
                raise

    def _flush(self, session_id):
        error = self._blocked.get(session_id)
        pending = self._pending.get(session_id, [])
        if error is None and pending:
            try:
                self._append(session_id, pending)
            except OSError as exc:
                error = f"Cannot save diagnostics {self._path(session_id)}: {exc}"
            else:
                pending.clear()
                self._reported.pop(session_id, None)
        if error and self._reported.get(session_id) != error:
            self._reported[session_id] = error
            return error
        return None

    def capture(self, event):
        if event.session_id is None:
            return None
        with self._lock:
            try:
                self._load(event.session_id)
            except DiagnosticError:
                pass
            record = log_record(event)
            if record["id"] not in self._ids[event.session_id]:
                self._ids[event.session_id].add(record["id"])
                self._records[event.session_id].append(record)
                self._pending.setdefault(event.session_id, []).append(record)
            return self._flush(event.session_id)

    def retry(self):
        with self._lock:
            return [error for session_id in self._pending
                    if (error := self._flush(session_id))]

    def forget(self, session_id):
        """Successful session deletion also cancels pending diagnostic retries."""
        with self._lock:
            for values in (self._records, self._ids, self._pending, self._blocked, self._reported):
                values.pop(session_id, None)
