"""持久化团队：信箱、协议和队友线程。

每个队友都是运行独立 agent 循环的守护线程。Lead 与队友通过文件信箱协作；
计划和关闭审批按 request id 匹配，避免旧回复影响后续任务。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import core
from . import tasks
from .teammate_session import TeammateSession

# -- 消息总线与团队协议 --

def _mailbox_dir() -> Path:
    return core.WORKDIR / ".mailboxes"

def _mailbox_root() -> Path:
    return _mailbox_dir().resolve()


VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}


def is_valid_agent_name(name: str) -> bool:
    return bool(VALID_AGENT_NAME.fullmatch(name))


class MessageBus:
    def __init__(self):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def _path(self, agent: str) -> Path:
        if not is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (_mailbox_dir() / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(_mailbox_root()):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with self._changed:
            _mailbox_dir().mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        print(f"  \033[33m[bus] {from_agent} -> {to_agent}: "
              f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str,
                          timeout: float | None = None) -> list[dict]:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = (None if deadline is None
                             else deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)


BUS = MessageBus()
active_teammates: dict[str, str] = {}
plan_gates: dict[str, str] = {}
plan_request_ids: dict[str, str] = {}
plan_gate_versions: dict[str, int] = {}
team_lock = threading.RLock()


# -- 协议状态 --

@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    while True:
        request_id = f"req_{random.randint(0, 999999):06d}"
        if request_id not in pending_requests:
            return request_id


def match_response(response_type: str, request_id: str, approve: bool,
                   from_agent: str, to_agent: str) -> bool:
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            print(f"  \033[31m[protocol] unknown request_id: {request_id}\033[0m")
            return False
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }[state.type]
        if response_type != expected:
            print(f"  \033[31m[protocol] expected {expected}, "
                  f"got {response_type}\033[0m")
            return False
        if from_agent != state.target or to_agent != state.sender:
            print(f"  \033[31m[protocol] {request_id} responder mismatch\033[0m")
            return False
        if state.status != "pending":
            return False
        state.status = "approved" if approve else "rejected"
    icon = "approved" if approve else "rejected"
    color = "32" if approve else "31"
    print(f"  \033[{color}m[protocol] {state.type} {icon} "
          f"({request_id}: {state.status})\033[0m")
    return True


def consume_lead_inbox(route_protocol=True) -> list[dict]:
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False),
                               msg.get("from", ""), msg.get("to", ""))
    return msgs


def format_team_events(msgs: list[dict]) -> str:
    lines = []
    for msg in msgs:
        request_id = msg.get("metadata", {}).get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(
            f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}"
        )
    return "[Team events]\n" + "\n".join(lines)



# -- 团队任务分配 --

IDLE_SCAN_INTERVAL = 2.0


def scan_unclaimed_tasks() -> list[tasks.Task]:
    """返回可领取且 worktree 绑定可用的任务。"""
    with tasks.task_lock:
        ready = []
        for task in tasks.list_tasks():
            if (task.status != "pending" or task.owner is not None
                    or not tasks.can_start(task.id)):
                continue
            _, error = tasks.task_worktree_cwd(task)
            if not error:
                ready.append(task)
        return ready


def claim_next_task(name: str) -> tasks.Task | None:
    """领取第一个仍可用的任务，不重复分配。"""
    with tasks.task_lock:
        if tasks.teammate_assignments.get(name) or tasks._owner_in_progress(name):
            return None
    for task in scan_unclaimed_tasks():
        result = tasks.claim_task(task.id, owner=name)
        if result.startswith("Claimed "):
            return tasks.load_task(task.id)
    return None


def _last_assistant_text(content) -> str:
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


def current_work_identity(owner: str) -> tuple[int, str | None]:
    with tasks.task_lock:
        assignment = tasks.teammate_assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return tasks.assignment_versions.get(owner, 0), task_id


def authorize_teammate_tool(name: str, block) -> str | None:
    with tasks.task_lock:
        version = tasks.assignment_versions.get(name, 0)
        with team_lock:
            if plan_gate_versions.get(name, version) != version:
                if plan_gates.get(name) != "not_required":
                    plan_gates[name] = "required"
                plan_request_ids.pop(name, None)
            plan_gate_versions[name] = version
    gate = plan_gates.get(name, "not_required")
    if (block.name in {"bash", "write_file", "edit_file", "complete_task"}
            and gate not in {"not_required", "approved"}):
        return f"Blocked: plan status is {gate}."
    return None


def create_teammate_session(name: str, system: str = ""):
    """Wire a session to live assignment and protocol operations."""
    return TeammateSession(
        name, core.client, core.MODEL, system,
        lambda block: authorize_teammate_tool(name, block),
        lambda to, content: _teammate_send_message(name, to, content),
        lambda plan: _teammate_submit_plan(name, plan),
    )


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """只接受 Lead 对该队友当前计划的回复。"""
    metadata = msg.get("metadata", {})
    request_id = metadata.get("request_id", "")
    with tasks.task_lock:
        work_version, task_id = current_work_identity(name)
        with team_lock:
            state = pending_requests.get(request_id)
            expected_id = plan_request_ids.get(name)
            valid = (
                msg.get("from") == "lead"
                and msg.get("to") == name
                and request_id == expected_id
                and state is not None
                and state.type == "plan_approval"
                and state.sender == name
                and state.target == "lead"
                and state.work_version == work_version
                and state.task_id == task_id
                and state.status in {"approved", "rejected"}
                and metadata.get("approve", False)
                == (state.status == "approved")
            )
            if not valid:
                return False, "[Ignored plan response: request mismatch]"
            plan_gates[name] = state.status
            plan_gate_versions[name] = work_version
            active_teammates[name] = "working"
            plan_request_ids.pop(name, None)
            outcome = state.status
    return True, f"[Plan {outcome}] {msg['content']}"


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """只接受 Lead 发给该队友且仍待处理的关闭请求。"""
    request_id = msg.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    with team_lock:
        if to != "lead" and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"



# -- 队友线程 --

def spawn_teammate_thread(name: str, role: str, prompt: str,
                          task_id: str | None = None,
                          require_plan: bool = False) -> str:
    if not is_valid_agent_name(name):
        return ("Invalid teammate name: use 1-64 letters, digits, "
                "underscores, or dashes")
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with tasks.task_lock:
        with team_lock:
            if any(existing.casefold() == name.casefold()
                   for existing in active_teammates):
                return f"Teammate '{name}' already exists"
            active_teammates[name] = "working"
            plan_gates[name] = "required" if require_plan else "not_required"
            tasks.assignment_versions[name] = 0
            plan_gate_versions[name] = 0

    if task_id:
        try:
            claimed = tasks.claim_task(task_id, owner=name)
        except (FileNotFoundError, ValueError) as exc:
            claimed = f"Error: {exc}"
        if not claimed.startswith("Claimed "):
            with tasks.task_lock:
                with team_lock:
                    active_teammates.pop(name, None)
                    plan_gates.pop(name, None)
                    plan_gate_versions.pop(name, None)
                    tasks.assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"

    system = (f"You are '{name}', a {role}. "
              "Use tools to complete tasks. "
              "You can list and claim tasks from the board. If the initial "
              "message contains [Assigned task], it is already claimed; do not "
              "call claim_task for it again. "
              "The runtime runs every filesystem tool in the claimed task's "
              "working directory. When asked for a plan, submit it before "
              "bash, write_file, or edit_file and wait for approval. The runtime "
              "delivers your final text to Lead. Use send_message only for "
              "intermediate coordination, and address the coordinator as 'lead'.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        if msg_type == "shutdown_request":
            accepted, notice = apply_shutdown_request(name, msg)
            if not accepted:
                messages.append({"role": "user", "content": notice})
                return False
            req_id = notice
            BUS.send(name, "lead", "Shutting down gracefully.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "
                  f"({req_id})\033[0m")
            return True

        if msg_type == "plan_approval_response":
            _, notice = apply_plan_response(name, msg)
            messages.append({"role": "user",
                "content": notice})
        elif msg_type == "plan_request":
            messages.append({"role": "user",
                "content": f"[Plan required] {msg['content']}"})
        elif msg_type == "message":
            messages.append({"role": "user",
                "content": f"[Message from {msg['from']}] {msg['content']}"})
        return False

    def run_loop():
        initial_prompt = prompt
        if task_id:
            task = tasks.load_task(task_id)
            initial_prompt += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {tasks.assignment_cwd(name)}"
            )
        if require_plan:
            initial_prompt += ("\n\n[Plan required] Submit a plan and wait for "
                               "Lead approval before bash, write_file, or edit_file.")
        messages = [{"role": "user", "content": initial_prompt}]
        session = create_teammate_session(name, system)

        should_stop = False
        while not should_stop:
            for msg in BUS.read_inbox(name):
                if handle_inbox_message(name, msg, messages):
                    should_stop = True
                    break
            if should_stop:
                break
            with team_lock:
                active_teammates[name] = "working"
            try:
                response = session.run(messages)
            except Exception as exc:
                BUS.send(name, "lead",
                         f"{type(exc).__name__}: {exc}", "error")
                break
            if any(block.type == "tool_use" for block in response.content):
                continue

            summary = _last_assistant_text(response.content)
            gate = plan_gates.get(name, "not_required")
            if gate != "pending" and summary:
                BUS.send(name, "lead", summary, "result")
            if gate == "pending":
                with team_lock:
                    active_teammates[name] = "waiting_approval"
            else:
                if tasks.release_completed_assignment(name):
                    plan_gates[name] = "not_required"
                    plan_request_ids.pop(name, None)
                    plan_gate_versions[name] = tasks.assignment_versions[name]
                with team_lock:
                    active_teammates[name] = "idle"
                BUS.send(name, "lead", "Waiting for more work.",
                         "idle_notification")

            while True:
                inbox = BUS.wait_for_messages(name, IDLE_SCAN_INTERVAL)
                if inbox:
                    for msg in inbox:
                        if handle_inbox_message(name, msg, messages):
                            should_stop = True
                            break
                    if should_stop or messages[-1]["role"] == "user":
                        break
                    continue

                task = claim_next_task(name)
                if not task:
                    continue
                try:
                    workdir = str(tasks.assignment_cwd(name))
                except (FileNotFoundError, ValueError) as exc:
                    workdir = f"unavailable ({exc})"
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Auto-claimed task {task.id}] "
                        f"{task.subject}\n{task.description}\n"
                        f"Work directory: {workdir}"
                    ),
                })
                print(f"  \033[32m[idle] {name} claimed "
                      f"{task.id}: {task.subject}\033[0m")
                break

    def run():
        try:
            run_loop()
        except Exception as exc:
            try:
                BUS.send(name, "lead", f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            try:
                tasks.release_teammate_assignment(name)
            except Exception as exc:
                try:
                    BUS.send(
                        name, "lead",
                        f"Assignment cleanup failed: {type(exc).__name__}: {exc}",
                        "error",
                    )
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                plan_gate_versions.pop(name, None)
                plan_request_ids.pop(name, None)
            print(f"  \033[32m[teammate] {name} finished\033[0m")

    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
    )


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    with tasks.task_lock:
        assignment = tasks.teammate_assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = tasks.assignment_versions.get(from_name, 0)
        with team_lock:
            if plan_gates.get(from_name) == "pending":
                return "A plan is already waiting for review."
            req_id = new_request_id()
            pending_requests[req_id] = ProtocolState(
                request_id=req_id, type="plan_approval",
                sender=from_name, target="lead",
                status="pending", payload=plan,
                work_version=work_version, task_id=task_id)
            plan_gates[from_name] = "pending"
            plan_request_ids[from_name] = req_id
            active_teammates[from_name] = "waiting_approval"
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Wait for Lead's decision."



# -- Lead 团队工具 --

def run_request_shutdown(teammate: str) -> str:
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        req_id = new_request_id()
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=teammate,
            status="pending", payload="")
    BUS.send("lead", teammate, "Finish the current step and shut down.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request -> {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown requested from {teammate} ({req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        plan_gates[teammate] = "required"
    BUS.send("lead", teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    with tasks.task_lock:
        work_version, task_id = current_work_identity(state.sender)
        with team_lock:
            state = pending_requests.get(request_id)
            if not state:
                return f"Request {request_id} not found"
            if state.type != "plan_approval":
                return f"Request {request_id} is not a plan"
            if state.status != "pending":
                return f"Request {request_id} already {state.status}"
            if state.work_version != work_version or state.task_id != task_id:
                return f"Request {request_id} belongs to an earlier assignment"
            if plan_request_ids.get(state.sender) != request_id:
                return f"Request {request_id} is not the current plan"
            state.status = "approved" if approve else "rejected"
    content = feedback or ("Plan approved." if approve
                           else "Revise the plan and submit it again.")
    BUS.send("lead", state.sender, content,
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "approved" if approve else "rejected"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {state.status} ({request_id})"
