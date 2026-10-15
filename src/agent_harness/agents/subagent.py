"""Unified child lifetimes, mailboxes and coordination protocols.

Once executes synchronously; persistent runs a daemon thread with a mailbox.
Plan and shutdown protocols match request IDs to the current work.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from ..tools.tool_result import ToolResult
from .. import core
from ..runtime import interaction
from ..workspace import tasks
from ..context import prompts
from ..providers import message_content
from ..providers.llm_client import APIConnectionError, StreamStopped
from .subagent_session import SubagentBudgetExceeded, SubagentSession, selected_tool_names
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
    usage: dict = field(default_factory=lambda: {"tokens": 0, "model_calls": 0,
                                               "input_tokens": 0, "output_tokens": 0})
    agent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    description: str = ""
    max_rounds: int = core.SUBAGENT_MAX_ROUNDS
    max_tool_calls: int = core.SUBAGENT_MAX_TOOL_CALLS
    rounds: int = 0
    tool_calls: int = 0
    latest_result: dict | None = None
    error: str = ""
    reason: str = ""
    last_text: str = ""
    finalizing: bool = False
    pending_next_work: bool = False


_active_subagents: dict[str, SubagentRecord] = {}
_subagent_results: dict[str, dict] = {}
_subagent_lock = threading.RLock()
_subagent_changed = threading.Condition(_subagent_lock)
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


def _record_for(identifier: str) -> SubagentRecord | None:
    """Resolve a public name or stable internal agent ID."""
    with _subagent_lock:
        if not isinstance(identifier, str):
            return None
        record = _active_subagents.get(identifier)
        if record is not None:
            return record
        for item in _active_subagents.values():
            if item.agent_id == identifier or item.name.casefold() == str(identifier).casefold():
                return item
    return None


def _snapshot(record: SubagentRecord, *, status: str | None = None,
              text: str | None = None) -> dict:
    return {
        "agent_id": record.agent_id,
        "name": record.name,
        "description": record.description,
        "role": record.role,
        "mode": record.mode,
        "status": status or record.status,
        "text": text if text is not None else record.last_text,
        "error": record.error,
        "reason": record.reason,
        "rounds": record.rounds,
        "max_rounds": record.max_rounds,
        "tool_calls": record.tool_calls,
        "max_tool_calls": record.max_tool_calls,
        "usage": dict(record.usage),
    }


def _status_snapshot(record: SubagentRecord) -> dict:
    """状态事件和列表共用同一快照，不包含任务正文。"""
    payload = _snapshot(record)
    payload.pop("text")
    return payload


def _clear_work_result(record: SubagentRecord, status: str):
    """接受新工作时同步失效旧结果，保留身份、历史和累计用量。"""
    with _subagent_changed:
        record.latest_result = None
        record.error = ""
        record.reason = ""
        record.last_text = ""
        record.rounds = 0
        record.tool_calls = 0
        record.status = status
        _subagent_results.pop(record.agent_id, None)
        _subagent_changed.notify_all()


def _queue_work(record: SubagentRecord):
    """按最终回答边界区分当前工作的消息与下一项工作。"""
    if record.finalizing:
        record.pending_next_work = True
        return
    if record.status == "idle" or (record.latest_result is not None and
                                   record.status in {"completed", "budget_exhausted"}):
        _clear_work_result(record, "pending")


def _save_result(record: SubagentRecord, status: str, text: str | None = None,
                 error: str = "", reason: str = "") -> dict:
    with _subagent_changed:
        record.error = error
        record.reason = reason or error or ("Completed" if status == "completed" else "")
        if text is not None:
            record.last_text = text
        record.latest_result = _snapshot(record, status=status)
        _subagent_results[record.agent_id] = deepcopy(record.latest_result)
        _subagent_changed.notify_all()
        return deepcopy(record.latest_result)


def _notify_result(record: SubagentRecord, snapshot: dict):
    """Deliver the same terminal snapshot that output queries return."""
    kind = {"completed": "result", "failed": "error"}.get(
        snapshot["status"], snapshot["status"])
    content = snapshot["text"]
    if snapshot["error"] or snapshot["status"] == "stopped":
        content = f"{snapshot['reason']}\n{content}".strip()
    _BUS.send(record.name, "lead", content, kind, deepcopy(snapshot))


def _publish_status(record: SubagentRecord, status: str | None = None, **extra):
    with _subagent_changed:
        if status is not None:
            record.status = status
        payload = _status_snapshot(record)
        _subagent_changed.notify_all()
    interaction.publish("subagent_status", **{**payload, **extra})


def get_subagent_output(agent_id: str, *, block: bool = True,
                        timeout: float = 30) -> dict:
    """Return a child result without exposing its message history."""
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("agent_id must be a non-empty string")
    if not isinstance(block, bool):
        raise ValueError("block must be a boolean")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite non-negative number")
    deadline = time.monotonic() + min(float(timeout), 60)
    with _subagent_changed:
        while True:
            result = _subagent_results.get(agent_id)
            record = next((item for item in _active_subagents.values()
                           if item.agent_id == agent_id), None)
            if result is not None:
                return deepcopy(result)
            if record is None:
                return {"agent_id": agent_id, "status": "not_found", "text": ""}
            if not block or record.latest_result is not None:
                return _snapshot(record)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**_snapshot(record), "timed_out": True}
            if interaction.stopped():
                return {**_snapshot(record), "interrupted": True}
            _subagent_changed.wait(min(remaining, 0.1))


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
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        if metadata.get("agent_id"):
            suffix += f" agent_id={metadata['agent_id']} description={metadata.get('description', '')}"
        lines.append(
            f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}"
        )
        if "usage" in metadata:
            usage = metadata["usage"]
            lines.append(
                f"Rounds {metadata['rounds']}/{metadata['max_rounds']}; "
                f"tools {metadata['tool_calls']}/{metadata['max_tool_calls']}; "
                f"input {usage['input_tokens']}, output {usage['output_tokens']}, "
                f"total {usage['tokens']} tokens.")
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
        record = _record_for(name)
        if record is not None:
            name = record.name
        return _PROTOCOL.authorize(name, block.name, tasks.assignment_identity(name))


def create_subagent_session(name: str, system: str = "", *,
                            mode="persistent", task_tools=True, tools=None,
                            client=None, model=None, max_tokens=None,
                            max_rounds=None, max_tool_calls=None, progress=None):
    """Build one session with runtime callbacks; do not start its lifecycle."""
    return SubagentSession(
        name, core.client if client is None else client, core.MODEL if model is None else model, system,
        lambda block: authorize_subagent_tool(name, block),
        lambda to, content: ToolResult(send_message(name, to, content)),
        lambda plan: ToolResult(submit_plan(name, plan)),
        mode=mode, task_tools=task_tools, tools=tools, max_tokens=max_tokens,
        max_rounds=max_rounds, max_tool_calls=max_tool_calls, progress=progress,
        work_context=lambda: f"Plan status: {_PROTOCOL.gate(name)}",
    )


def apply_plan_response(name: str, msg: dict) -> tuple[bool, str]:
    """只接受 Lead 针对子 agent当前任务和当前计划的审批回复。

    输入：name 为子 agent名称；msg 为含 request_id 和 approve 的回复消息。
    返回：(是否接受, 提示文本)。身份、任务 ID、版本、请求 ID 和状态均匹配时，
        更新计划门禁；仍在等待审批的工作恢复 working，已结束的工作保持空闲。
        移除当前计划请求索引，不匹配时返回忽略提示。
        批准和拒绝都会被接受，但拒绝不会开放工具权限。
    """
    with _team_lock():
        record = _record_for(name)
        if record is None:
            return False, "[Ignored plan response: request mismatch]"
        name = record.name
        identity = tasks.assignment_identity(name)
        accepted, notice = _PROTOCOL.apply_plan(name, msg, identity)
        if accepted and record.status == "waiting_approval" and record.latest_result is None:
            record.status = "working"
        return accepted, notice


def apply_shutdown_request(name: str, msg: dict) -> tuple[bool, str]:
    """验证 Lead 发给当前子 agent的待处理关闭请求。

    输入：name 为子 agent名称；msg 为含 request_id 的关闭请求消息。
    返回：有效时为 (True, 请求 ID)，并将子 agent标记为 stopping。
        无效或重复请求返回 (False, 忽略提示)，不在这里发送确认或退出线程。
    """
    with _subagent_lock:
        record = _record_for(name)
        if record is None or record.status == "stopping":
            return False, "[Ignored shutdown request: request mismatch]"
        accepted, notice = _PROTOCOL.accept_shutdown(record.name, msg)
        if accepted:
            record.status = "stopping"
        return accepted, notice


def send_message(from_name: str, to: str, content: str) -> str:
    """Deliver a model-selected message to Lead or a live persistent child."""
    with _subagent_lock:
        target = _record_for(to)
        sender = _record_for(from_name)
        if sender is not None:
            from_name = sender.name
        if to != "lead" and target is None:
            raise ValueError(f"Agent '{to}' is not active")
        if to != "lead" and (target.mode != "persistent" or target.status == "stopping"):
            raise ValueError(f"Agent '{to}' cannot receive messages in its current mode/status")
        _BUS.send(from_name, target.name if target is not None else "lead", content,
                  metadata=({"agent_id": sender.agent_id, "name": sender.name,
                             "description": sender.description} if sender is not None else None))
        if target is not None:
            _queue_work(target)
    return f"Sent to {target.name if target is not None else 'lead'}"


def list_subagents() -> list[dict]:
    """Return public metadata for active instances without exposing history."""
    with _subagent_lock:
        return [_status_snapshot(item)
                for item in sorted(_active_subagents.values(), key=lambda item: item.name)]


class _StopSubagent(Exception):
    """Internal signal for an accepted shutdown request."""

    def __init__(self, reason="Stopped by Lead"):
        super().__init__(reason)


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
                         "shutdown_response", {"request_id": notice, "approve": True,
                                               "agent_id": record.agent_id, "name": record.name,
                                               "description": record.description})
                raise _StopSubagent()
            # A stale protocol message must not wake an idle model.
            continue
        if kind == "plan_approval_response":
            accepted, notice = apply_plan_response(record.name, msg)
            if not accepted:
                continue
            with _subagent_lock:
                record.messages.append({"role": "user", "content": notice})
                # 审批只恢复仍在等待的工作；已交付终态的工作不会获得新预算。
                wake = wake or (record.status == "working" and record.latest_result is None)
        elif kind in {"message", "plan_request"}:
            content = msg.get("content", "")
            prefix = "[Plan required]" if kind == "plan_request" else f"[Message from {msg['from']}]"
            with _team_lock():
                # 与发送方共用锁，接收线程不能先于新委派的结果失效继续执行。
                if kind == "plan_request":
                    _PROTOCOL.require_plan(record.name, tasks.assignment_identity(record.name)[0])
                if record.status in {"idle", "pending"}:
                    _queue_work(record)
                    new_work.append(content)
                record.messages.append({"role": "user", "content": f"{prefix} {content}"})
                wake = True
    if new_work:
        with _subagent_lock:
            record.active_request = "\n\n".join(new_work)
    return wake


def _wait_for_work(record, session):
    """Wait for useful input or shutdown; never scan or auto-claim the board."""
    while True:
        inbox = _BUS.wait_for_messages(record.name)
        if _receive(record, session, inbox):
            return


def _execute_work(record, session):
    """Run a delegation through shared sessions until a complete final answer."""
    with _team_lock():
        if record.mode == "persistent" and record.status == "pending":
            # 启动前接受的后续消息合并到同一委派，当前上下文不会遗漏其中的要求。
            accepted = record.active_request
            _receive(record, session, _BUS.read_inbox(record.name))
            if record.active_request != accepted:
                record.active_request = f"{accepted}\n\n{record.active_request}"
        _clear_work_result(record, "working")
    session.begin_work(record.active_request)

    def before_iteration():
        try:
            if record.mode == "once" and interaction.stopped():
                raise _StopSubagent("Interrupted by user")
            if record.mode == "persistent":
                _receive(record, session, _BUS.read_inbox(record.name))
                session.check_budget()
                if _PROTOCOL.gate(record.name) == "pending":
                    with session.engine.waiting_approval():
                        while _PROTOCOL.gate(record.name) == "pending":
                            _publish_status(record, "waiting_approval")
                            _wait_for_work(record, session)
                            session.check_budget()
        except _StopSubagent as exc:
            # 共享执行机登记 stopped，生命周期层再还原关闭协议的原因。
            raise StreamStopped(str(exc)) from exc
        record.rounds = session.rounds
        record.tool_calls = session.tool_calls
        _publish_status(record, "working")

    def before_complete():
        try:
            with _team_lock():
                wake = _receive(record, session, _BUS.read_inbox(record.name))
                if not wake:
                    # Serialize the final-answer boundary with messages being queued.
                    record.finalizing = True
                return wake
        except _StopSubagent as exc:
            raise StreamStopped(str(exc)) from exc

    try:
        response = session.run(record.messages, before_iteration=before_iteration,
                               before_complete=(before_complete
                                                if record.mode == "persistent" else None))
    except StreamStopped as exc:
        raise _StopSubagent(str(exc)) from exc
    finally:
        record.rounds = session.rounds
        record.tool_calls = session.tool_calls
        record.last_text = session.last_text
    if record.mode == "once" and interaction.stopped():
        raise _StopSubagent("Interrupted by user")
    return message_content.extract_text(response.content)


def _cleanup(record):
    """Release unfinished ownership and remove runtime/protocol registration."""
    try:
        tasks.release_assignment(record.name)
    finally:
        with _subagent_changed:
            _active_subagents.pop(record.name, None)
            _PROTOCOL.clear(record.name)
            _subagent_changed.notify_all()
        _BUS.read_inbox(record.name)


def _persistent_loop(record, session):
    """Keep one history alive across work and message waits in this process."""
    try:
        while True:
            try:
                summary = _execute_work(record, session)
            except SubagentBudgetExceeded as exc:
                status, summary = "budget_exhausted", None
                error = f"{exc.kind} budget exhausted ({exc.limit})"
            else:
                with tasks.task_lock:
                    assignment = tasks.assignments.get(record.name)
                    unfinished = (assignment and tasks.load_task(
                        str(assignment["task_id"])).status == "in_progress")
                if unfinished:
                    summary = f"[Task unfinished; assignment retained]\n{summary}"
                status, summary = "completed", summary or "Finished without a text summary."
                error = ""
            with _subagent_changed:
                snapshot = _save_result(record, status, summary, error=error)
                _publish_status(record, status)
            _notify_result(record, snapshot)
            with _team_lock():
                assignment = tasks.assignments.get(record.name)
                unfinished = (assignment and tasks.load_task(
                    str(assignment["task_id"])).status == "in_progress")
                released = tasks.release_completed_assignment(record.name)
                if released or not assignment:
                    if not assignment:
                        tasks.advance_assignment_version(record.name)
                    _PROTOCOL.reset_work(record.name, tasks.assignment_identity(record.name)[0])
                next_work = record.pending_next_work
                record.pending_next_work = False
                record.finalizing = False
                if next_work:
                    _clear_work_result(record, "pending")
                else:
                    with _subagent_changed:
                        record.status = "idle"
                        _subagent_changed.notify_all()
            # Publish the state chosen under the same lock used by message delivery.
            _publish_status(record)
            # A message queued after the completion boundary already represents next work.
            with _subagent_lock:
                if record.status != "pending":
                    _BUS.send(record.name, "lead",
                             "Waiting for guidance on the unfinished Task." if unfinished else "Waiting for more work.",
                             "idle_notification", {"agent_id": record.agent_id, "name": record.name,
                                                    "description": record.description})
            _wait_for_work(record, session)
    except _StopSubagent as exc:
        snapshot = _save_result(record, "stopped", reason=str(exc))
        _publish_status(record, "stopped")
        _notify_result(record, snapshot)
    except (APIConnectionError, ConnectionError, TimeoutError) as exc:
        snapshot = _save_result(record, "network_error", error=f"{type(exc).__name__}: {exc}")
        _publish_status(record, "network_error")
        _notify_result(record, snapshot)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        snapshot = _save_result(record, "failed", error=error)
        _publish_status(record, "failed")
        _notify_result(record, snapshot)
    finally:
        try:
            _cleanup(record)
        except Exception as exc:
            error = f"Cleanup failed: {type(exc).__name__}: {exc}"
            snapshot = _save_result(record, "failed", error=error)
            _publish_status(record, "failed")
            _notify_result(record, snapshot)


def spawn_subagent(name: str, prompt: str, mode: str, role: str = "coder",
                   task_id: str | None = None, require_plan: bool = False, tools=None,
                   *, description: str, client=None, model=None,
                   max_tokens=None, max_rounds=None, max_tool_calls=None) -> dict:
    """Create one child and execute according to its fixed lifetime.

    Input: a unique name, delegation, explicit once/persistent mode, optional
        role, Task, plan requirement, tool whitelist and execution dependencies.
    Return: identity, status, text, reason, counts and usage. Once returns its final result; persistent
        returns started and delivers later results through the mailbox.
    """
    valid_name = name if is_valid_agent_name(name) and name.casefold() not in RESERVED_SUBAGENT_NAMES else None
    valid_mode = mode if isinstance(mode, str) and mode in {"once", "persistent"} else None
    result = {"agent_id": None, "name": valid_name,
              "description": description if isinstance(description, str) else None,
              "mode": valid_mode, "status": "failed", "text": "", "error": "", "reason": "",
              "usage": {"tokens": 0, "model_calls": 0,
                        "input_tokens": 0, "output_tokens": 0}}
    def reject(reason):
        return {**result, "text": reason, "error": reason, "reason": reason}

    if valid_name is None:
        return reject("Invalid or reserved subagent name")
    if valid_mode is None:
        return reject("mode must be once or persistent")
    if not isinstance(description, str) or not description.strip() or len(description.split()) > 8:
        return reject("description must be a short non-empty task description")
    if not isinstance(prompt, str) or not prompt.strip():
        return reject("prompt must be non-empty")
    if (not isinstance(role, str) or not role.strip()
            or not isinstance(require_plan, bool)
            or (task_id is not None and (not isinstance(task_id, str) or not task_id))):
        return reject("Invalid role, task_id or require_plan")
    if mode == "once" and require_plan:
        return reject("once cannot wait for plan approval")
    task_tools = task_id is not None or mode == "persistent"
    try:
        selected = selected_tool_names(tools, mode=mode, task_tools=task_tools)
        if require_plan and "submit_plan" not in selected:
            raise ValueError("require_plan needs the submit_plan tool")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer")
        if max_rounds is not None and (type(max_rounds) is not int or max_rounds <= 0):
            raise ValueError("max_rounds must be a positive integer")
        if max_tool_calls is not None and (type(max_tool_calls) is not int or max_tool_calls <= 0):
            raise ValueError("max_tool_calls must be a positive integer")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a non-empty string")
    except ValueError as exc:
        return reject(str(exc))
    record = SubagentRecord(
        name, role, mode, active_request=prompt, description=description,
        max_rounds=core.SUBAGENT_MAX_ROUNDS if max_rounds is None else max_rounds,
        max_tool_calls=(core.SUBAGENT_MAX_TOOL_CALLS if max_tool_calls is None
                        else max_tool_calls))
    with _team_lock():
        if any(key.casefold() == name.casefold() for key in _active_subagents):
            return reject(f"Subagent '{name}' already exists")
        if tasks.assignments.get(name) or tasks._owner_in_progress(name):
            return reject(f"Subagent '{name}' already owns a Task")
        _active_subagents[name] = record
        _PROTOCOL.register(name, require_plan, tasks.assignment_identity(name)[0])
    session = None
    parent = interaction.current_context()
    context = parent.delegate(name, "persistent" if mode == "persistent" else None) if parent else None
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
            "Do not spawn agents or create/remove worktrees. "
            "Follow your current delegation. An [Assigned task] is already claimed. "
            "Each workspace tool uses the current assigned directory, or the startup directory "
            "when no Task is assigned. Return a concise final summary of the work. "
        )
        if "bash" in selected:
            system += f"Environment: {prompts.ENVIRONMENT_PROMPT}. "
        if "claim_task" in selected:
            system += ("Before working on a new Task, explicitly call claim_task; never assume ownership. "
                       "You cannot claim another Task before the current work ends. ")
        if "complete_task" in selected:
            system += ("Complete the assigned Task after preserving its results. "
                       "complete_task retains the directory until your final answer. ")
        if "submit_plan" in selected:
            system += "When a plan is required, submit_plan and wait for approval before changes. "
        if mode == "persistent":
            if "send_message" in selected:
                system += ("Decide whether intermediate coordination is useful and call send_message "
                           "to 'lead' or another active persistent child. ")
            system += ("The runtime delivers your "
                       "final answer and waits for the next message; do not poll while idle. ")
        def progress(value):
            with _subagent_lock:
                record.rounds = value.rounds
                record.tool_calls = value.tool_calls
                record.last_text = value.last_text
                record.usage = dict(value.usage)
            _publish_status(record)

        session = create_subagent_session(
            name, system, mode=mode, task_tools=task_tools, tools=tools,
            client=client, model=model, max_tokens=max_tokens,
            max_rounds=record.max_rounds, max_tool_calls=record.max_tool_calls,
            progress=progress)
        record.usage = session.usage
        if mode == "persistent":
            result = _snapshot(record, status="started",
                               text="Started; the runtime will deliver results and idle events.")
            def run_persistent():
                with interaction.execution_context(context):
                    _persistent_loop(record, session)
            thread = threading.Thread(target=run_persistent, daemon=True)
            thread.start()
            if context is not None:
                with _subagent_lock:
                    _persistent_threads[:] = [item for item in _persistent_threads if item.is_alive()]
                    _persistent_threads.append(thread)
            return result
        with interaction.execution_context(context):
            try:
                summary = _execute_work(record, session)
                assignment = tasks.assignments.get(name)
                if assignment and tasks.load_task(str(assignment["task_id"])).status != "completed":
                    raise RuntimeError("Task is unfinished; ownership released to pending")
                result = _save_result(record, "completed", summary or "Finished without a text summary.")
            finally:
                record.rounds = session.rounds
                record.tool_calls = session.tool_calls
                record.last_text = session.last_text or record.last_text
    except SubagentBudgetExceeded as exc:
        reason = f"{exc.kind} budget exhausted ({exc.limit})"
        result = _save_result(record, "budget_exhausted", error=reason)
    except _StopSubagent as exc:
        result = _save_result(record, "stopped", reason=str(exc))
    except (APIConnectionError, ConnectionError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = _save_result(record, "network_error", error=error)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = _save_result(record, "failed", record.last_text or error, error)
    try:
        _cleanup(record)
    except Exception as exc:
        error = f"Cleanup failed: {type(exc).__name__}: {exc}"
        result = _save_result(record, "failed", error=error)
    with interaction.execution_context(context):
        _publish_status(record, result["status"])
    return result


def submit_plan(from_name: str, plan: str) -> str:
    """为子 agent当前分配创建计划审批请求并投递给 Lead。

    输入：from_name 为提交子 agent；plan 为计划正文。
    返回：提交确认和新 request_id；已有 pending 计划时拒绝重复提交。
        保存当前任务 ID 和分配版本，将计划设为 pending、子 agent设为 waiting_approval。
    """
    with _team_lock():
        record = _record_for(from_name)
        if record is None or record.status == "stopping":
            raise ValueError(f"Subagent '{from_name}' is not active")
        from_name = record.name
        identity = tasks.assignment_identity(from_name)
        state, notice = _PROTOCOL.submit_plan(from_name, plan, identity)
        if state is None:
            raise ValueError(notice)
        req_id = state.request_id
        record.status = "waiting_approval"
    _BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id, "agent_id": record.agent_id,
              "name": record.name, "description": record.description})
    return f"Plan submitted ({req_id}). Wait for Lead's decision."



# -- Lead 团队工具 --

def run_request_shutdown(name: str) -> ToolResult:
    """以 Lead 身份向活跃子 agent发起有请求 ID 的关闭协议。

    输入：name 为目标子 agent名称。
    返回：请求确认或子 agent不活跃的提示。登记 pending 请求并写入信箱，
        不直接终止线程，等待子 agent处理关闭请求并回复确认。
    """
    with _subagent_lock:
        record = _record_for(name)
        if record is None:
            return ToolResult(f"Subagent '{name}' is not active", "failed")
        if record.mode != "persistent":
            return ToolResult(f"Subagent '{name}' requires persistent mode", "failed")
        req_id = _PROTOCOL.request_shutdown(record.name).request_id
    _BUS.send("lead", record.name, "Finish the current step and shut down.",
             "shutdown_request",
             {"request_id": req_id})
    core.terminal_print(f"  \033[35m[protocol] shutdown_request -> {record.name} "
          f"({req_id})\033[0m")
    return ToolResult(f"Shutdown requested from {name} ({req_id})", "success")


def run_request_plan(name: str, task: str) -> ToolResult:
    """以 Lead 身份要求子 agent先为指定工作提交计划。

    输入：name 为目标子 agent；task 为计划要求的正文，不是任务 ID。
    返回：投递确认或子 agent不活跃的提示。将门禁设为 required 并发送 plan_request；
        审批请求 ID 会在子 agent真正提交计划时生成。
    """
    with _subagent_lock:
        record = _record_for(name)
        if record is None:
            return ToolResult(f"Subagent '{name}' is not active", "failed")
        if record.mode != "persistent":
            return ToolResult(f"Subagent '{name}' requires persistent mode", "failed")
        _BUS.send("lead", record.name, task, "plan_request")
        _PROTOCOL.require_plan(record.name)
        _queue_work(record)
    return ToolResult(f"Plan requested from {record.name}", "success")


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
