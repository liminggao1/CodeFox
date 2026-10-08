"""Unified child lifetimes, mailboxes and coordination protocols.

Once executes synchronously; persistent runs a daemon thread with a mailbox.
Plan and shutdown protocols match request IDs to the current work.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import core
from . import tasks
from . import prompts
from . import message_content
from .subagent_session import SubagentSession, selected_tool_names

# -- 消息总线与团队协议 --

def _mailbox_dir() -> Path:
    return core.WORKDIR / ".mailboxes"

def _mailbox_root() -> Path:
    return _mailbox_dir().resolve()


VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_SUBAGENT_NAMES = {"lead", "agent"}


def is_valid_agent_name(name: str) -> bool:
    return isinstance(name, str) and bool(VALID_AGENT_NAME.fullmatch(name))


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
        """向文件信箱追加一条消息，并唤醒当前总线中等待收信的线程。

        输入：from_agent/to_agent 为发送者和收件人；content 为正文；
            msg_type 为消息类型；metadata 为可选的 request_id、审批结果等信息。
        返回：None。写入消息及时间戳并打印投递日志，文件错误继续抛出。
        """
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
        """在线程锁内读取并清空指定信箱。

        输入：agent 为收件人名称。
        返回：消息字典列表；无信箱时为空。成功消费后删除文件。
        """
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str,
                          timeout: float | None = None) -> list[dict]:
        """阻塞等待信箱出现内容，再读取并消费消息。

        输入：agent 为收件人；timeout 为最多等待的秒数，None 表示持续等待。
        返回：收到的消息列表；超时返回空列表。使用单调时钟计算剩余等待时间。
        """
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
@dataclass
class SubagentRecord:
    name: str
    role: str = "coder"
    mode: str = "persistent"
    status: str = "working"
    messages: list = field(default_factory=list)
    active_request: str = ""
    usage: dict = field(default_factory=lambda: {"tokens": 0, "model_calls": 0})


active_subagents: dict[str, SubagentRecord] = {}
plan_gates: dict[str, str] = {}
plan_request_ids: dict[str, str] = {}
plan_gate_versions: dict[str, int] = {}
subagent_lock = threading.RLock()


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
    """匹配待处理协议请求与回复，防止错误身份或重复回复改变状态。

    输入：response_type 为回复类型；request_id 为请求 ID；approve 为是否批准；
        from_agent/to_agent 为回复的发送者和收件人。
    返回：类型、身份和 pending 状态均匹配时返回 True，并记录 approved 或 rejected。
        不匹配时返回 False；计划对应的任务版本由计划处理函数进一步校验。
    """
    with subagent_lock:
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
    """消费 Lead 信箱，并按需将协议回复交给请求匹配器。

    输入：route_protocol 为是否处理带 request_id 的协议回复，默认开启。
    返回：本次收到的全部消息列表，包括协议消息；读取成功后信箱已被清空。
    """
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            # Exited children have already cleared their protocol indices.
            # Their acknowledgement remains an event for Lead to consume.
            if req_id in pending_requests and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False),
                               msg.get("from", ""), msg.get("to", ""))
    return msgs


def format_subagent_events(msgs: list[dict]) -> str:
    """将团队消息转换为可加入主模型历史的事件文本。

    输入：msgs 为含 type、from、content 和可选 metadata 的消息列表。
    返回：带 [Subagent events] 前缀的字符串，每条消息包含类型、发送者和可选请求 ID。
    """
    lines = []
    for msg in msgs:
        request_id = msg.get("metadata", {}).get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(
            f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}"
        )
    return "[Subagent events]\n" + "\n".join(lines)



# -- Current work and protocol authorization --

def current_work_identity(owner: str) -> tuple[int, str | None]:
    with tasks.task_lock:
        assignment = tasks.assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return tasks.assignment_versions.get(owner, 0), task_id


def authorize_subagent_tool(name: str, block) -> str | None:
    """根据子 agent当前任务的计划审批状态决定是否允许工具执行。

    输入：name 为子 agent名称；block 为含 name 的工具请求。
    返回：放行时为 None，未获批准时为拒绝文本。
        分配版本变化会清理旧请求，并让需要审批的计划重新进入 required 状态。
        bash、写入、编辑和完成任务需获批；此处放行后仍需经过通用权限 hooks。
    """
    with tasks.task_lock:
        version = tasks.assignment_versions.get(name, 0)
        with subagent_lock:
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


def create_subagent_session(name: str, system: str = "", *,
                            mode="persistent", task_tools=True, tools=None,
                            client=None, model=None, max_tokens=None):
    """Build one session with runtime callbacks; do not start its lifecycle."""
    return SubagentSession(
        name, core.client if client is None else client, core.MODEL if model is None else model, system,
        lambda block: authorize_subagent_tool(name, block),
        lambda to, content: send_message(name, to, content),
        lambda plan: submit_plan(name, plan),
        mode=mode, task_tools=task_tools, tools=tools, max_tokens=max_tokens,
        work_context=lambda: f"Plan status: {plan_gates.get(name, 'not_required')}",
    )


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """只接受 Lead 针对子 agent当前任务和当前计划的审批回复。

    输入：name 为子 agent名称；msg 为含 request_id 和 approve 的回复消息。
    返回：(是否接受, 提示文本)。身份、任务 ID、版本、请求 ID 和状态均匹配时，
        更新计划门禁、将子 agent登记为 working，并移除当前计划请求索引。
        不匹配时返回忽略提示；批准和拒绝都会被接受，但拒绝不会开放工具权限。
    """
    metadata = msg.get("metadata", {})
    request_id = metadata.get("request_id", "")
    with tasks.task_lock:
        work_version, task_id = current_work_identity(name)
        with subagent_lock:
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
            active_subagents[name].status = "working"
            plan_request_ids.pop(name, None)
            outcome = state.status
    return True, f"[Plan {outcome}] {msg['content']}"


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """验证 Lead 发给当前子 agent的待处理关闭请求。

    输入：name 为子 agent名称；msg 为含 request_id 的关闭请求消息。
    返回：有效时为 (True, 请求 ID)，并将子 agent标记为 stopping。
        无效或重复请求返回 (False, 忽略提示)，不在这里发送确认或退出线程。
    """
    request_id = msg.get("metadata", {}).get("request_id", "")
    with subagent_lock:
        state = pending_requests.get(request_id)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and name in active_subagents
            and active_subagents[name].status != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_subagents[name].status = "stopping"
    return True, request_id


def send_message(from_name: str, to: str, content: str) -> str:
    """Deliver a model-selected message to Lead or a live persistent child."""
    with subagent_lock:
        target = active_subagents.get(to)
        if to != "lead" and target is None:
            return f"Agent '{to}' is not active"
        if to != "lead" and (target.mode != "persistent" or target.status == "stopping"):
            return f"Agent '{to}' cannot receive messages in its current mode/status"
        BUS.send(from_name, to, content)
    return f"Sent to {to}"


def list_subagents() -> list[dict]:
    """Return public metadata for active instances without exposing history."""
    with subagent_lock:
        return [{"name": item.name, "role": item.role, "mode": item.mode,
                 "status": item.status}
                for item in sorted(active_subagents.values(), key=lambda item: item.name)]


class _StopSubagent(Exception):
    """Internal signal for an accepted shutdown request."""


def _receive(record, session, messages):
    """Route protocol messages and append coordination to the same history."""
    new_work = []
    wake = False
    for msg in messages:
        kind = msg.get("type", "message")
        if kind == "shutdown_request":
            accepted, notice = apply_shutdown_request(record.name, msg)
            if accepted:
                BUS.send(record.name, "lead", "Shutting down gracefully.",
                         "shutdown_response", {"request_id": notice, "approve": True})
                raise _StopSubagent()
            # A stale protocol message must not wake an idle model.
            continue
        if kind == "plan_approval_response":
            accepted, notice = apply_plan_response(record.name, msg)
            if not accepted:
                continue
            record.messages.append({"role": "user", "content": notice})
            wake = True
        elif kind in {"message", "plan_request"}:
            content = msg.get("content", "")
            prefix = "[Plan required]" if kind == "plan_request" else f"[Message from {msg['from']}]"
            record.messages.append({"role": "user", "content": f"{prefix} {content}"})
            wake = True
            if record.status == "idle" and not tasks.assignments.get(record.name):
                new_work.append(content)
    if new_work:
        record.active_request = "\n\n".join(new_work)
        session.begin_work(record.active_request)
    return wake


def _wait_for_work(record, session):
    """Wait for useful input or shutdown; never scan or auto-claim the board."""
    while True:
        inbox = BUS.wait_for_messages(record.name)
        if _receive(record, session, inbox):
            return


def _execute_work(record, session):
    """Run a delegation through shared sessions until a complete final answer."""
    round_index = 0
    while record.mode == "persistent" or round_index < core.SUBAGENT_MAX_ROUNDS:
        round_index += 1
        if record.mode == "persistent":
            _receive(record, session, BUS.read_inbox(record.name))
            while plan_gates.get(record.name) == "pending":
                record.status = "waiting_approval"
                _wait_for_work(record, session)
        record.status = "working"
        response = session.run(record.messages)
        if not message_content.has_tool_use(response.content):
            return message_content.extract_text(response.content)
    raise RuntimeError("Subagent reached its round limit without completing")


def _clear_protocol(name):
    plan_gates.pop(name, None)
    plan_gate_versions.pop(name, None)
    plan_request_ids.pop(name, None)
    for request_id, state in list(pending_requests.items()):
        if name in {state.sender, state.target}:
            pending_requests.pop(request_id, None)


def _cleanup(record):
    """Release unfinished ownership and remove runtime/protocol registration."""
    try:
        tasks.release_assignment(record.name)
    finally:
        with subagent_lock:
            active_subagents.pop(record.name, None)
            _clear_protocol(record.name)
        BUS.read_inbox(record.name)


def _persistent_loop(record, session):
    """Keep one history alive across work and message waits in this process."""
    try:
        while True:
            summary = _execute_work(record, session)
            with tasks.task_lock:
                assignment = tasks.assignments.get(record.name)
                unfinished = (assignment and tasks.load_task(
                    str(assignment["task_id"])).status == "in_progress")
            if unfinished:
                summary = f"[Task unfinished; assignment retained]\n{summary}"
            BUS.send(record.name, "lead", summary or "Finished without a text summary.", "result")
            released = tasks.release_completed_assignment(record.name)
            if released or not assignment:
                if not assignment:
                    tasks.advance_assignment_version(record.name)
                with subagent_lock:
                    plan_gates[record.name] = "not_required"
                    plan_request_ids.pop(record.name, None)
                    plan_gate_versions[record.name] = tasks.assignment_versions.get(record.name, 0)
            record.status = "idle"
            BUS.send(record.name, "lead",
                     "Waiting for guidance on the unfinished Task." if unfinished else "Waiting for more work.",
                     "idle_notification")
            _wait_for_work(record, session)
    except _StopSubagent:
        pass
    except Exception as exc:
        BUS.send(record.name, "lead", f"{type(exc).__name__}: {exc}", "error")
    finally:
        try:
            _cleanup(record)
        except Exception as exc:
            BUS.send(record.name, "lead", f"Cleanup failed: {type(exc).__name__}: {exc}", "error")


def spawn_subagent(name: str, prompt: str, mode: str, role: str = "coder",
                   task_id: str | None = None, require_plan: bool = False, tools=None,
                   *, client=None, model=None, max_tokens=None) -> dict:
    """Create one child and execute according to its fixed lifetime.

    Input: a unique name, delegation, explicit once/persistent mode, optional
        role, Task and plan requirement (persistent only).
    Return: name/mode/status/text. Once returns its final result; persistent
        returns started and delivers later results through the mailbox.
    """
    valid_name = name if is_valid_agent_name(name) and name.casefold() not in RESERVED_SUBAGENT_NAMES else None
    valid_mode = mode if isinstance(mode, str) and mode in {"once", "persistent"} else None
    result = {"name": valid_name, "mode": valid_mode, "status": "failed", "text": "",
              "usage": {"tokens": 0, "model_calls": 0}}
    if valid_name is None:
        result["text"] = "Invalid or reserved subagent name"
        return result
    if valid_mode is None:
        result["text"] = "mode must be once or persistent"
        return result
    if not isinstance(prompt, str) or not prompt.strip():
        result["text"] = "prompt must be non-empty"
        return result
    if (not isinstance(role, str) or not role.strip()
            or not isinstance(require_plan, bool)
            or (task_id is not None and (not isinstance(task_id, str) or not task_id))):
        result["text"] = "Invalid role, task_id or require_plan"
        return result
    if mode == "once" and require_plan:
        result["text"] = "once cannot wait for plan approval"
        return result
    task_tools = task_id is not None or mode == "persistent"
    try:
        selected_tool_names(tools, mode=mode, task_tools=task_tools)
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a non-empty string")
    except ValueError as exc:
        result["text"] = str(exc)
        return result
    record = SubagentRecord(name, role, mode, active_request=prompt)
    with tasks.task_lock:
        with subagent_lock:
            if any(key.casefold() == name.casefold() for key in active_subagents):
                result["text"] = f"Subagent '{name}' already exists"
                return result
            if tasks.assignments.get(name) or tasks._owner_in_progress(name):
                result["text"] = f"Subagent '{name}' already owns a Task"
                return result
            active_subagents[name] = record
            plan_gates[name] = "required" if require_plan else "not_required"
            plan_gate_versions[name] = tasks.assignment_versions.get(name, 0)
    session = None
    try:
        if task_id is not None:
            claimed = tasks.claim_task(task_id, owner=name)
            if not claimed.startswith("Claimed "):
                raise ValueError(claimed)
        initial = prompt
        if task_id is not None:
            task = tasks.load_task(task_id)
            initial += (f"\n\n[Assigned task {task.id}] {task.subject}\n"
                        f"{task.description}\nWork directory: {tasks.assignment_cwd(name)}")
        if require_plan:
            initial += "\n\n[Plan required] Submit a plan and wait for Lead approval before changes."
        record.messages.append({"role": "user", "content": initial})
        system = (
            f"You are '{name}', a {role} coding subagent at {core.WORKDIR}. "
            f"Environment: {prompts.ENVIRONMENT_PROMPT}. Do not spawn agents. "
            "Follow your current delegation. An [Assigned task] is already claimed. "
            "Before working on a new Task, explicitly call claim_task; never assume ownership. "
            "Each workspace tool uses the current assigned directory, or the startup directory "
            "when no Task is assigned. Complete the assigned Task after preserving its results, "
            "then return a concise final summary. complete_task retains the directory until "
            "your final answer. You cannot claim another Task before that work ends. "
            "When a plan is required, submit it and wait for approval before changes. "
        )
        if mode == "persistent":
            system += ("Decide whether intermediate coordination is useful and call send_message "
                       "to 'lead' or another active persistent child. The runtime delivers your "
                       "final answer and waits for the next message; do not poll while idle. ")
        session = create_subagent_session(name, system, mode=mode, task_tools=task_tools,
                                           tools=tools, client=client, model=model, max_tokens=max_tokens)
        record.usage = session.usage
        session.begin_work(prompt)
        if mode == "persistent":
            threading.Thread(target=lambda: _persistent_loop(record, session), daemon=True).start()
            result.update(status="started", text="Started; the runtime will deliver results and idle events.")
            return result
        summary = _execute_work(record, session)
        assignment = tasks.assignments.get(name)
        if assignment and tasks.load_task(str(assignment["task_id"])).status != "completed":
            raise RuntimeError("Task is unfinished; ownership released to pending")
        result.update(status="completed", text=summary or "Finished without a text summary.")
    except Exception as exc:
        result["text"] = f"{type(exc).__name__}: {exc}"
    try:
        _cleanup(record)
    except Exception as exc:
        result.update(status="failed", text=f"Cleanup failed: {type(exc).__name__}: {exc}")
    if session is not None:
        result["usage"] = dict(session.usage)
    return result


def submit_plan(from_name: str, plan: str) -> str:
    """为子 agent当前分配创建计划审批请求并投递给 Lead。

    输入：from_name 为提交子 agent；plan 为计划正文。
    返回：提交确认和新 request_id；已有 pending 计划时拒绝重复提交。
        保存当前任务 ID 和分配版本，将计划设为 pending、子 agent设为 waiting_approval。
    """
    with tasks.task_lock:
        assignment = tasks.assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = tasks.assignment_versions.get(from_name, 0)
        with subagent_lock:
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
            active_subagents[from_name].status = "waiting_approval"
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Wait for Lead's decision."



# -- Lead 团队工具 --

def run_request_shutdown(name: str) -> str:
    """以 Lead 身份向活跃子 agent发起有请求 ID 的关闭协议。

    输入：name 为目标子 agent名称。
    返回：请求确认或子 agent不活跃的提示。登记 pending 请求并写入信箱，
        不直接终止线程，等待子 agent处理关闭请求并回复确认。
    """
    if name not in active_subagents:
        return f"Subagent '{name}' is not active"
    if active_subagents[name].mode != "persistent":
        return f"Subagent '{name}' requires persistent mode"
    with subagent_lock:
        req_id = new_request_id()
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=name,
            status="pending", payload="")
    BUS.send("lead", name, "Finish the current step and shut down.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request -> {name} "
          f"({req_id})\033[0m")
    return f"Shutdown requested from {name} ({req_id})"


def run_request_plan(name: str, task: str) -> str:
    """以 Lead 身份要求子 agent先为指定工作提交计划。

    输入：name 为目标子 agent；task 为计划要求的正文，不是任务 ID。
    返回：投递确认或子 agent不活跃的提示。将门禁设为 required 并发送 plan_request；
        审批请求 ID 会在子 agent真正提交计划时生成。
    """
    if name not in active_subagents:
        return f"Subagent '{name}' is not active"
    if active_subagents[name].mode != "persistent":
        return f"Subagent '{name}' requires persistent mode"
    with subagent_lock:
        plan_gates[name] = "required"
        plan_request_ids.pop(name, None)
    BUS.send("lead", name, task, "plan_request")
    return f"Plan requested from {name}"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    """审核子 agent当前分配的待处理计划，并发送批准或拒绝回复。

    输入：request_id 为计划请求 ID；approve 为是否批准；feedback 为可选反馈正文。
    返回：审批结果或拒绝处理的原因。只更新当前任务、版本和计划索引匹配的请求；
        将协议状态设为 approved 或 rejected 后投递回复，子 agent收信时再更新工具门禁。
    """
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    with tasks.task_lock:
        work_version, task_id = current_work_identity(state.sender)
        with subagent_lock:
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
