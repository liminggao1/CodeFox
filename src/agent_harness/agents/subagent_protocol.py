"""Own child plan gates and plan/shutdown requests, without lifecycle or I/O."""

from dataclasses import dataclass, field, replace
import random
import time


@dataclass
class _Plan:
    gate: str
    work_version: int
    request_id: str | None = None


@dataclass(frozen=True)
class ProtocolRequest:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


class SubagentProtocol:
    """Serialize protocol transitions using the caller's existing runtime RLock."""

    def __init__(self, lock):
        self._lock = lock
        self._plans: dict[str, _Plan] = {}
        self._requests: dict[str, ProtocolRequest] = {}

    def register(self, name, require_plan, work_version):
        with self._lock:
            self._plans[name] = _Plan("required" if require_plan else "not_required", work_version)

    def gate(self, name):
        with self._lock:
            plan = self._plans.get(name)
            return plan.gate if plan else "not_required"

    def request(self, request_id):
        """Return an immutable request snapshot, or None after cleanup."""
        with self._lock:
            return self._requests.get(request_id)

    def empty(self):
        with self._lock:
            return not self._plans and not self._requests

    def clear(self, name=None):
        """Remove one child's protocol state, or all state for a fresh runtime."""
        with self._lock:
            if name is None:
                self._plans.clear()
                self._requests.clear()
            else:
                self._plans.pop(name, None)
                self._requests = {key: state for key, state in self._requests.items()
                                  if name not in {state.sender, state.target}}

    def reset_work(self, name, work_version):
        with self._lock:
            self._plans[name] = _Plan("not_required", work_version)

    def require_plan(self, name, work_version=None):
        with self._lock:
            plan = self._plans.setdefault(name, _Plan("not_required", 0))
            plan.gate = "required"
            plan.request_id = None
            if work_version is not None:
                plan.work_version = work_version

    def authorize(self, name, tool_name, identity):
        """Invalidate stale approval before checking a mutation against its gate."""
        version, _task_id = identity
        with self._lock:
            plan = self._plans.get(name)
            if plan is None:
                return None
            if plan.work_version != version:
                if plan.gate != "not_required":
                    plan.gate = "required"
                plan.request_id = None
                plan.work_version = version
            if (tool_name in {"bash", "write_file", "edit_file", "complete_task"}
                    and plan.gate not in {"not_required", "approved"}):
                return f"Blocked: plan status is {plan.gate}."
        return None

    def _new_request(self, kind, sender, target, payload, identity=(None, None)):
        while True:
            request_id = f"req_{random.randint(0, 999999):06d}"
            if request_id not in self._requests:
                break
        version, task_id = identity
        state = ProtocolRequest(request_id, kind, sender, target, "pending", payload,
                                version, task_id)
        self._requests[request_id] = state
        return state

    def submit_plan(self, name, payload, identity):
        """Return the new request or the existing duplicate-submission notice."""
        with self._lock:
            plan = self._plans[name]
            if plan.gate == "pending":
                return None, "A plan is already waiting for review."
            state = self._new_request("plan_approval", name, "lead", payload, identity)
            plan.gate = "pending"
            plan.request_id = state.request_id
            plan.work_version = identity[0]
            return state, ""

    def review_plan(self, request_id, approve, identity):
        """Decide the current request; the child gate stays pending until delivery."""
        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                return None, f"Request {request_id} not found"
            if state.type != "plan_approval":
                return None, f"Request {request_id} is not a plan"
            if state.status != "pending":
                return None, f"Request {request_id} already {state.status}"
            if (state.work_version, state.task_id) != identity:
                return None, f"Request {request_id} belongs to an earlier assignment"
            plan = self._plans.get(state.sender)
            if plan is None or plan.request_id != request_id:
                return None, f"Request {request_id} is not the current plan"
            state = replace(state, status="approved" if approve else "rejected")
            self._requests[request_id] = state
            return state, ""

    def apply_plan(self, name, message, identity):
        """Apply only a matching decision for the current request and assignment."""
        metadata = message.get("metadata", {})
        request_id = metadata.get("request_id", "")
        with self._lock:
            state = self._requests.get(request_id)
            plan = self._plans.get(name)
            valid = (
                message.get("type") == "plan_approval_response"
                and message.get("from") == "lead" and message.get("to") == name
                and state is not None and plan is not None
                and request_id == plan.request_id
                and state.type == "plan_approval"
                and state.sender == name and state.target == "lead"
                and (state.work_version, state.task_id) == identity
                and state.status in {"approved", "rejected"}
                and metadata.get("approve", False) == (state.status == "approved")
            )
            if not valid:
                return False, "[Ignored plan response: request mismatch]"
            plan.gate = state.status
            plan.work_version = identity[0]
            plan.request_id = None
            return True, f"[Plan {state.status}] {message['content']}"

    def request_shutdown(self, name):
        with self._lock:
            return self._new_request("shutdown", "lead", name, "")

    def accept_shutdown(self, name, message):
        request_id = message.get("metadata", {}).get("request_id", "")
        with self._lock:
            state = self._requests.get(request_id)
            valid = (
                message.get("type") == "shutdown_request"
                and message.get("from") == "lead" and message.get("to") == name
                and state is not None and state.type == "shutdown"
                and state.sender == "lead" and state.target == name
                and state.status == "pending"
            )
            return (True, request_id) if valid else (
                False, "[Ignored shutdown request: request mismatch]")

    def match_response(self, response_type, request_id, approve, from_agent, to_agent):
        """Match a pending reply without owning its delivery or printing events."""
        with self._lock:
            state = self._requests.get(request_id)
            if state is None:
                return False
            expected = {"shutdown": "shutdown_response", "plan_approval": "plan_approval_response"}[state.type]
            if (response_type != expected or from_agent != state.target
                    or to_agent != state.sender or state.status != "pending"):
                return False
            self._requests[request_id] = replace(state, status="approved" if approve else "rejected")
            return True
