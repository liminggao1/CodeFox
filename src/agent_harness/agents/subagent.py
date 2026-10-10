"""Unified child lifetimes, mailboxes and coordination protocols.

Once executes synchronously; persistent runs a daemon thread with a mailbox.
Plan and shutdown protocols match request IDs to the current work.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..tools.tool_result import ToolResult
from .. import core
from ..runtime import interaction
from ..workspace import tasks
from ..context import prompts
from ..providers import message_content
from .subagent_session import SubagentSession, selected_tool_names
from .subagent_protocol import SubagentProtocol

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
        core.terminal_print(f"  \033[33m[bus] {from_agent} -> {to_agent}: "
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


_BUS = MessageBus()


@contextmanager
def _team_lock():
    """团队状态唯一锁序：先任务表锁，再子 agent 锁。

    所有同时触碰 `tasks` 与子 agent 注册表的写入点都经此进入，锁序只在这里定义。
    """
    with tasks.task_lock:
        with _subagent_lock:
            yield


@dataclass
class SubagentRecord:
    name: str
    role: str = "coder"
    mode: str = "persistent"
    status: str = "working"
    messages: list = field(default_factory=list)
    active_request: str = ""
    usage: dict = field(default_factory=lambda: {"tokens": 0, "model_calls": 0})


_active_subagents: dict[str, SubagentRecord] = {}
_subagent_lock = threading.RLock()
_PROTOCOL = SubagentProtocol(_subagent_lock)
_persistent_threads: list[threading.Thread] = []


# -- 子 agent 操作 interface：调用方不再持有注册表、锁、总线或协议对象 --

def has_pending_inbox() -> bool:
    """Lead 信箱是否有待消费内容；替代调用方直接查询消息总线。"""
    return _BUS.peek("lead")


def active_names() -> tuple[str, ...]:
    """当前活跃子 agent 的名称快照；供运行时构造上下文时读取。"""
    with _subagent_lock:
        return tuple(_active_subagents)


def wait_for_subagents():
    with _subagent_lock:
        threads = list(_persistent_threads)
    for thread in threads:
        thread.join()
    with _subagent_lock:
        _persistent_threads.clear()


# -- 协议状态 --

def consume_lead_inbox(route_protocol=True) -> list[dict]:
    """消费 Lead 信箱，并按需将协议回复交给请求匹配器。

    输入：route_protocol 为是否处理带 request_id 的协议回复，默认开启。
    返回：本次收到的全部消息列表，包括协议消息；读取成功后信箱已被清空。
    """
    msgs = _BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            # Exited children have already cleared their protocol indices.
            # Their acknowledgement remains an event for Lead to consume.
            if msg_type.endswith("_response"):
                matched = _PROTOCOL.match_response(
                    msg_type, req_id, meta.get("approve", False),
                    msg.get("from", ""), msg.get("to", ""))
                if matched:
                    state = _PROTOCOL.request(req_id)
                    if state is not None:
                        color = "32" if state.status == "approved" else "31"
                        core.terminal_print(f"  \033[{color}m[protocol] {state.type} "
                                            f"{state.status} ({req_id}: {state.status})\033[0m")
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

def authorize_subagent_tool(name: str, block) -> str | None:
    """根据子 agent当前任务的计划审批状态决定是否允许工具执行。

    输入：name 为子 agent名称；block 为含 name 的工具请求。
    返回：放行时为 None，未获批准时为拒绝文本。
        分配版本变化会清理旧请求，并让需要审批的计划重新进入 required 状态。
        bash、写入、编辑和完成任务需获批；此处放行后仍需经过通用权限 hooks。
    """
    with _team_lock():
        return _PROTOCOL.authorize(name, block.name, tasks.assignment_identity(name))


def create_subagent_session(name: str, system: str = "", *,
                            mode="persistent", task_tools=True, tools=None,
                            client=None, model=None, max_tokens=None):
    """Build one session with runtime callbacks; do not start its lifecycle."""
    return SubagentSession(
        name, core.client if client is None else client, core.MODEL if model is None else model, system,
        lambda block: authorize_subagent_tool(name, block),
        lambda to, content: ToolResult(send_message(name, to, content)),
        lambda plan: ToolResult(submit_plan(name, plan)),
        mode=mode, task_tools=task_tools, tools=tools, max_tokens=max_tokens,
        work_context=lambda: f"Plan status: {_PROTOCOL.gate(name)}",
    )


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """只接受 Lead 针对子 agent当前任务和当前计划的审批回复。

    输入：name 为子 agent名称；msg 为含 request_id 和 approve 的回复消息。
    返回：(是否接受, 提示文本)。身份、任务 ID、版本、请求 ID 和状态均匹配时，
        更新计划门禁、将子 agent登记为 working，并移除当前计划请求索引。
        不匹配时返回忽略提示；批准和拒绝都会被接受，但拒绝不会开放工具权限。
    """
    with _team_lock():
        identity = tasks.assignment_identity(name)
        if name not in _active_subagents:
            return False, "[Ignored plan response: request mismatch]"
        accepted, notice = _PROTOCOL.apply_plan(name, msg, identity)
        if accepted:
            _active_subagents[name].status = "working"
        return accepted, notice


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """验证 Lead 发给当前子 agent的待处理关闭请求。

    输入：name 为子 agent名称；msg 为含 request_id 的关闭请求消息。
    返回：有效时为 (True, 请求 ID)，并将子 agent标记为 stopping。
        无效或重复请求返回 (False, 忽略提示)，不在这里发送确认或退出线程。
    """
    with _subagent_lock:
        record = _active_subagents.get(name)
        if record is None or record.status == "stopping":
            return False, "[Ignored shutdown request: request mismatch]"
        accepted, notice = _PROTOCOL.accept_shutdown(name, msg)
        if accepted:
            record.status = "stopping"
        return accepted, notice


def send_message(from_name: str, to: str, content: str) -> str:
    """Deliver a model-selected message to Lead or a live persistent child."""
    with _subagent_lock:
        target = _active_subagents.get(to)
        if to != "lead" and target is None:
            raise ValueError(f"Agent '{to}' is not active")
        if to != "lead" and (target.mode != "persistent" or target.status == "stopping"):
            raise ValueError(f"Agent '{to}' cannot receive messages in its current mode/status")
        _BUS.send(from_name, to, content)
    return f"Sent to {to}"


def list_subagents() -> list[dict]:
    """Return public metadata for active instances without exposing history."""
    with _subagent_lock:
        return [{"name": item.name, "role": item.role, "mode": item.mode,
                 "status": item.status}
                for item in sorted(_active_subagents.values(), key=lambda item: item.name)]


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
                _BUS.send(record.name, "lead", "Shutting down gracefully.",
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
        inbox = _BUS.wait_for_messages(record.name)
        if _receive(record, session, inbox):
            return


def _execute_work(record, session):
    """Run a delegation through shared sessions until a complete final answer."""
    round_index = 0
    while record.mode == "persistent" or round_index < core.SUBAGENT_MAX_ROUNDS:
        round_index += 1
        if record.mode == "persistent":
            _receive(record, session, _BUS.read_inbox(record.name))
            while _PROTOCOL.gate(record.name) == "pending":
                record.status = "waiting_approval"
                _wait_for_work(record, session)
        record.status = "working"
        interaction.publish("subagent_status", name=record.name, mode=record.mode, status=record.status)
        response = session.run(record.messages)
        if not message_content.has_tool_use(response.content):
            return message_content.extract_text(response.content)
    raise RuntimeError("Subagent reached its round limit without completing")


def _cleanup(record):
    """Release unfinished ownership and remove runtime/protocol registration."""
    try:
        tasks.release_assignment(record.name)
    finally:
        with _subagent_lock:
            _active_subagents.pop(record.name, None)
            _PROTOCOL.clear(record.name)
        _BUS.read_inbox(record.name)


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
            _BUS.send(record.name, "lead", summary or "Finished without a text summary.", "result")
            released = tasks.release_completed_assignment(record.name)
            if released or not assignment:
                if not assignment:
                    tasks.advance_assignment_version(record.name)
                with _team_lock():
                    _PROTOCOL.reset_work(record.name, tasks.assignment_identity(record.name)[0])
            record.status = "idle"
            interaction.publish("subagent_status", name=record.name, mode=record.mode, status=record.status)
            _BUS.send(record.name, "lead",
                     "Waiting for guidance on the unfinished Task." if unfinished else "Waiting for more work.",
                     "idle_notification")
            _wait_for_work(record, session)
    except _StopSubagent:
        pass
    except Exception as exc:
        _BUS.send(record.name, "lead", f"{type(exc).__name__}: {exc}", "error")
    finally:
        try:
            _cleanup(record)
        except Exception as exc:
            _BUS.send(record.name, "lead", f"Cleanup failed: {type(exc).__name__}: {exc}", "error")


def spawn_subagent(name: str, prompt: str, mode: str, role: str = "coder",
                   task_id: str | None = None, require_plan: bool = False, tools=None,
                   *, client=None, model=None, max_tokens=None) -> dict:
    """Create one child and execute according to its fixed lifetime.

    Input: a unique name, delegation, explicit once/persistent mode, optional
        role, Task, plan requirement, tool whitelist and execution dependencies.
    Return: name/mode/status/text/usage. Once returns its final result; persistent
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
    with _team_lock():
        if any(key.casefold() == name.casefold() for key in _active_subagents):
            result["text"] = f"Subagent '{name}' already exists"
            return result
        if tasks.assignments.get(name) or tasks._owner_in_progress(name):
            result["text"] = f"Subagent '{name}' already owns a Task"
            return result
        _active_subagents[name] = record
        _PROTOCOL.register(name, require_plan, tasks.assignment_identity(name)[0])
    session = None
    try:
        if task_id is not None:
            tasks.claim_task(task_id, owner=name)
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
            parent = interaction.current_context()
            context = parent.delegate(name, "persistent") if parent else None
            def run_persistent():
                with interaction.execution_context(context):
                    _persistent_loop(record, session)
                    interaction.publish("subagent_status", name=name, mode=mode, status="closed", usage=dict(session.usage))
            thread = threading.Thread(target=run_persistent, daemon=True)
            thread.start()
            if context is not None:
                with _subagent_lock:
                    _persistent_threads[:] = [item for item in _persistent_threads if item.is_alive()]
                    _persistent_threads.append(thread)
            result.update(status="started", text="Started; the runtime will deliver results and idle events.")
            return result
        parent = interaction.current_context()
        context = parent.delegate(name) if parent else None
        with interaction.execution_context(context):
            summary = _execute_work(record, session)
            interaction.publish("subagent_status", name=name, mode=mode, status="completed", usage=dict(session.usage))
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
    with _team_lock():
        identity = tasks.assignment_identity(from_name)
        record = _active_subagents.get(from_name)
        if record is None or record.status == "stopping":
            raise ValueError(f"Subagent '{from_name}' is not active")
        state, notice = _PROTOCOL.submit_plan(from_name, plan, identity)
        if state is None:
            raise ValueError(notice)
        req_id = state.request_id
        record.status = "waiting_approval"
    _BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Wait for Lead's decision."



# -- Lead 团队工具 --

def run_request_shutdown(name: str) -> ToolResult:
    """以 Lead 身份向活跃子 agent发起有请求 ID 的关闭协议。

    输入：name 为目标子 agent名称。
    返回：请求确认或子 agent不活跃的提示。登记 pending 请求并写入信箱，
        不直接终止线程，等待子 agent处理关闭请求并回复确认。
    """
    with _subagent_lock:
        record = _active_subagents.get(name)
        if record is None:
            return ToolResult(f"Subagent '{name}' is not active", "failed")
        if record.mode != "persistent":
            return ToolResult(f"Subagent '{name}' requires persistent mode", "failed")
        req_id = _PROTOCOL.request_shutdown(name).request_id
    _BUS.send("lead", name, "Finish the current step and shut down.",
             "shutdown_request",
             {"request_id": req_id})
    core.terminal_print(f"  \033[35m[protocol] shutdown_request -> {name} "
          f"({req_id})\033[0m")
    return ToolResult(f"Shutdown requested from {name} ({req_id})", "success")


def run_request_plan(name: str, task: str) -> ToolResult:
    """以 Lead 身份要求子 agent先为指定工作提交计划。

    输入：name 为目标子 agent；task 为计划要求的正文，不是任务 ID。
    返回：投递确认或子 agent不活跃的提示。将门禁设为 required 并发送 plan_request；
        审批请求 ID 会在子 agent真正提交计划时生成。
    """
    with _subagent_lock:
        record = _active_subagents.get(name)
        if record is None:
            return ToolResult(f"Subagent '{name}' is not active", "failed")
        if record.mode != "persistent":
            return ToolResult(f"Subagent '{name}' requires persistent mode", "failed")
        _PROTOCOL.require_plan(name)
    _BUS.send("lead", name, task, "plan_request")
    return ToolResult(f"Plan requested from {name}", "success")


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> ToolResult:
    """审核子 agent当前分配的待处理计划，并发送批准或拒绝回复。

    输入：request_id 为计划请求 ID；approve 为是否批准；feedback 为可选反馈正文。
    返回：审批结果或拒绝处理的原因。只更新当前任务、版本和计划索引匹配的请求；
        将协议状态设为 approved 或 rejected 后投递回复，子 agent收信时再更新工具门禁。
    """
    state = _PROTOCOL.request(request_id)
    if not state:
        return ToolResult(f"Request {request_id} not found", "failed")
    with _team_lock():
        state, notice = _PROTOCOL.review_plan(request_id, approve, tasks.assignment_identity(state.sender))
        if state is None:
            return ToolResult(notice, "failed")
    content = feedback or ("Plan approved." if approve
                           else "Revise the plan and submit it again.")
    _BUS.send("lead", state.sender, content,
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "approved" if approve else "rejected"
    core.terminal_print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return ToolResult(f"Plan {state.status} ({request_id})", "success")
