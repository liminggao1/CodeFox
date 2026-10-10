"""Frontend-neutral notifications, execution context and approval rendezvous.

This module owns delivery only. Session history, permissions and execution
remain with their existing owners. All payloads crossing the seam are JSON data.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import threading
from typing import Literal
import uuid


# -- Execution and submission contracts --

EventScope = Literal["session", "runtime"]

DISPLAY_FACT_TYPES = frozenset({
    "user_message", "assistant_message", "tool_call", "tool_result",
    "todo_updated", "subagent_status", "background_status",
    "approval_requested", "approval_decided", "command_result", "turn_finished",
    "execution_status",
})

@dataclass(frozen=True)
class TurnOutcome:
    status: Literal["completed", "failed", "stopped", "truncated"]
    reason: str = ""


@dataclass(frozen=True)
class SubmissionResult:
    status: Literal["accepted", "busy", "invalid", "closed"]
    submission_id: str | None = None
    kind: Literal["command", "turn"] | None = None
    reason: str = ""


@dataclass(frozen=True)
class InteractionEvent:
    sequence: int
    session_id: str | None
    generation: int
    turn_id: str | None
    source: str
    type: str
    payload: dict
    scope: EventScope = "session"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    session_id: str | None
    generation: int
    turn_id: str
    source: str
    tool_name: str
    tool_call_id: str
    arguments: dict


ApprovalDecision = Literal["approve", "deny", "closed"]
ExecutionOrigin = Literal["frontend", "background", "persistent", "workflow"]


@dataclass(frozen=True)
class ExecutionContext:
    channel: InteractionChannel
    session_id: str | None
    generation: int
    turn_id: str | None = None
    source: str = "lead"
    origin: ExecutionOrigin = "background"
    stop_event: threading.Event | None = None
    scope: EventScope = "session"
    checkpoint: object = None
    model_call_id: str | None = None
    tool_call_id: str | None = None
    lead_context: object = None

    def stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def delegate(self, source: str, origin: ExecutionOrigin | None = None):
        """Synchronous once inherits origin; independent work sets it explicitly."""
        return replace(self, source=source, origin=origin or self.origin,
                       checkpoint=self.checkpoint if origin is None else None,
                       model_call_id=None, tool_call_id=None)

    def publish(self, event_type: str, **payload) -> InteractionEvent:
        return self.channel.publish(self, event_type, payload)


_context: ContextVar[ExecutionContext | None] = ContextVar("interaction_context", default=None)


def current_context() -> ExecutionContext | None:
    return _context.get()


@contextmanager
def execution_context(context: ExecutionContext | None):
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def publish(event_type: str, **payload) -> InteractionEvent | None:
    context = current_context()
    if context is not None:
        return context.publish(event_type, **payload)
    return None


@contextmanager
def operation_context(**fields):
    context = current_context()
    with execution_context(replace(context, **fields) if context is not None else None):
        yield


def operation_status(phase, name, state, *, operation_id=None, **payload):
    identity = operation_id or uuid.uuid4().hex
    publish("execution_status", phase=phase, name=name, state=state,
            operation_id=identity, **payload)
    return identity


@contextmanager
def observe_operation(phase, name):
    """Report an actual synchronous operation without changing its result."""
    identity = operation_status(phase, name, "running")
    try:
        yield
    except Exception as exc:
        operation_status(phase, name, "failed", operation_id=identity, reason=f"{type(exc).__name__}: {exc}")
        raise
    else:
        operation_status(phase, name, "completed", operation_id=identity)


def data_copy(value):
    """Reject live SDK/business objects and detach nested mutable JSON values."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


# -- One adapter's notification and approval channel --

class InteractionChannel:
    def __init__(self):
        self._changed = threading.Condition()
        self._events: deque[InteractionEvent] = deque()
        self._facts: deque[InteractionEvent] = deque()
        self._requests: deque[tuple[ApprovalRequest, int]] = deque()
        self._unacknowledged: dict[int, InteractionEvent] = {}
        self._delivered: set[int] = set()
        self._pending: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalDecision] = {}
        self._cancelled_turns: set[str] = set()
        self._sequence = 0
        self._generation = 0
        self._closed = False
        self._approvals_closed = False
        self._projector = None

    def set_projector(self, projector):
        """Install the runtime's pure fact projection before starting execution."""
        with self._changed:
            self._projector = projector

    def _stale(self, item) -> bool:
        return item.generation < self._generation

    def _advance_generation(self, context):
        if context.scope != "session" or context.generation <= self._generation:
            return
        self._generation = context.generation
        self._events = deque(event for event in self._events
                             if event.scope == "runtime" or not self._stale(event))
        for sequence, event in list(self._unacknowledged.items()):
            if event.scope == "session" and self._stale(event):
                del self._unacknowledged[sequence]
                self._delivered.discard(sequence)
        for request_id, request in self._pending.items():
            if self._stale(request):
                self._decisions[request_id] = "deny"

    def publish(self, context: ExecutionContext, event_type: str,
                payload: dict, *, deliver=True, capture=True) -> InteractionEvent:
        copied = data_copy(payload)
        for key in ("model_call_id", "tool_call_id"):
            value = getattr(context, key)
            if value is not None:
                copied.setdefault(key, value)
        with self._changed:
            if self._closed:
                raise RuntimeError("Interaction channel is closed")
            self._advance_generation(context)
            self._sequence += 1
            event = InteractionEvent(self._sequence, context.session_id,
                                     context.generation, context.turn_id,
                                     context.source, event_type, copied, context.scope)
            if context.scope == "runtime" or not self._stale(event):
                if deliver:
                    self._events.append(event)
                    self._unacknowledged[event.sequence] = event
                if capture and event.scope == "session" and event.type in DISPLAY_FACT_TYPES:
                    record = self._projector(event) if self._projector is not None else None
                    if record is not None:
                        self._sequence += 1
                        display = replace(event, sequence=self._sequence, type="display_record",
                                          payload={"record": data_copy(record)})
                        self._events.append(display)
                        self._unacknowledged[display.sequence] = display
                        self._facts.append(replace(display, payload=data_copy(display.payload)))
                    elif self._projector is None:
                        self._facts.append(replace(event, payload=data_copy(event.payload)))
            self._changed.notify_all()
            return replace(event, payload=data_copy(event.payload))

    def drain_facts(self) -> list[InteractionEvent]:
        """Separate backend intake from frontend delivery and display acknowledgments."""
        with self._changed:
            facts = list(self._facts)
            self._facts.clear()
        return [replace(event, payload=data_copy(event.payload)) for event in facts]

    def next_event(self, timeout: float = 0) -> InteractionEvent | None:
        """Return one notification; blocking reads belong to adapters, not publishers."""
        with self._changed:
            self._changed.wait_for(lambda: self._events or self._closed, timeout)
            if not self._events:
                return None
            event = self._events.popleft()
            self._delivered.add(event.sequence)
            return event

    def acknowledge_event(self, sequence: int) -> bool:
        """Confirm successful display, in delivery order; dequeue is not display."""
        with self._changed:
            if (sequence not in self._delivered
                    or sequence != next(iter(self._unacknowledged), None)):
                return False
            self._delivered.remove(sequence)
            del self._unacknowledged[sequence]
            self._changed.notify_all()
            return True

    def request_approval(self, context: ExecutionContext, tool_name: str,
                         tool_call_id: str, arguments: dict) -> ApprovalDecision:
        if context.scope != "session" or context.origin != "frontend" or context.turn_id is None:
            return "deny"
        request = ApprovalRequest(uuid.uuid4().hex, context.session_id,
                                  context.generation, context.turn_id,
                                  context.source, tool_name, tool_call_id,
                                  data_copy(arguments))
        with self._changed:
            if self._closed or self._approvals_closed:
                return "closed"
            self._advance_generation(context)
            if (self._stale(request) or context.stopped()
                    or context.turn_id in self._cancelled_turns):
                return "deny"
            self._pending[request.request_id] = request
        # The execution thread checkpoints outside the delivery lock, before
        # making a request actionable and while waiting for an adapter decision.
        try:
            if context.checkpoint is not None:
                context.publish("approval_requested", request_id=request.request_id,
                                tool_name=tool_name, tool_call_id=tool_call_id,
                                arguments=request.arguments)
                context.checkpoint()
            with self._changed:
                self._requests.append((request, self._sequence))
                self._changed.notify_all()
            while True:
                with self._changed:
                    if request.request_id in self._decisions:
                        decision = self._decisions.pop(request.request_id)
                        break
                    if self._closed or self._approvals_closed or context.stopped():
                        decision = "closed" if self._closed or self._approvals_closed else "deny"
                        break
                    self._changed.wait(timeout=0.1)
                if context.checkpoint is not None:
                    context.checkpoint(poll=True)
            if context.checkpoint is not None:
                # close() may detach an adapter while the execution thread wakes.
                if not self._closed:
                    context.publish("approval_decided", request_id=request.request_id,
                                    tool_name=tool_name, tool_call_id=tool_call_id,
                                    decision=decision)
                context.checkpoint()
            return decision
        finally:
            with self._changed:
                self._pending.pop(request.request_id, None)
                self._decisions.pop(request.request_id, None)

    def _approval_ready(self) -> bool:
        while self._requests:
            request, preceding = self._requests[0]
            if (request.request_id not in self._pending
                    or request.request_id in self._decisions or self._stale(request)):
                self._requests.popleft()
                continue
            first = next(iter(self._unacknowledged), None)
            return first is None or first > preceding
        return False

    def next_approval(self, timeout: float = 0) -> ApprovalRequest | None:
        with self._changed:
            self._changed.wait_for(lambda: self._approval_ready()
                                   or self._closed or self._approvals_closed, timeout)
            if self._closed or self._approvals_closed or not self._approval_ready():
                return None
            request, _ = self._requests.popleft()
            return replace(request, arguments=data_copy(request.arguments))

    def respond(self, request_id: str, generation: int,
                decision: ApprovalDecision) -> bool:
        if decision not in {"approve", "deny", "closed"}:
            raise ValueError("Unknown approval decision")
        with self._changed:
            request = self._pending.get(request_id)
            if (self._closed or self._approvals_closed or request is None or request.generation != generation
                    or self._stale(request)
                    or request_id in self._decisions):
                return False
            self._decisions[request_id] = decision
            self._changed.notify_all()
            return True

    def cancel_turn(self, turn_id: str) -> None:
        """Deny both pending and future requests belonging to a stopped turn."""
        with self._changed:
            self._cancelled_turns.add(turn_id)
            for request_id, request in self._pending.items():
                if request.turn_id == turn_id:
                    self._decisions[request_id] = "deny"
            self._changed.notify_all()

    def close(self) -> None:
        """Detach the adapter and release all approval waiters without approval."""
        with self._changed:
            self._closed = True
            for request_id in self._pending:
                self._decisions[request_id] = "closed"
            self._changed.notify_all()

    def close_approvals(self) -> None:
        """Closing execution still publishes notifications until runtime_closed."""
        with self._changed:
            self._approvals_closed = True
            for request_id in self._pending:
                self._decisions[request_id] = "closed"
            self._changed.notify_all()


def stopped() -> bool:
    context = current_context()
    return context is not None and context.stopped()
