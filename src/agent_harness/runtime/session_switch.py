"""会话切换的唯一拥有者：恢复事务的目标、顺序与失败回滚都在这里。

调用方只调 ``SessionSwitch.switch(target)``。读快照、校验目标、停服务、
切换目录、激活归属、重启服务和发布 ``session_changed`` 全在实现内完成；
``interactive_runtime`` 只注入自己的窄接口，不再持有这些步骤。
"""

from __future__ import annotations

from pathlib import Path
import os

from .. import core
from ..agents import goal
from ..agents import subagent as subagent_runtime
from ..tools import mcp_tools
from ..workspace import tasks
from . import interaction
from . import scheduler
from .session_store import SessionError


class SessionSwitch:
    """切换事务；依赖由运行时显式注入，不反向引用 InteractiveRuntime。"""

    def __init__(self, *, store, session, lead_context, channel, context,
                 checkpoint, record, pause_background, after_activate,
                 read_goal, replace_goal):
        self._store = store
        self._session = session
        self._lead_context = lead_context
        self._channel = channel
        self._context = context
        self._checkpoint = checkpoint
        self._record = record
        self._pause_background = pause_background
        self._after_activate = after_activate
        # goal 由运行时持有；这里只经注入的访问器读写，不直接引用 host 模块绑定。
        self._read_goal = read_goal
        self._replace_goal = replace_goal

    # -- 读取阶段：只读，不改任何状态 --

    def _prepare(self, target):
        """读取目标快照并校验；不改变当前 assignment、目录或会话。"""
        snapshot = self._store.load(target)
        current_goal = self._read_goal()
        restored_goal = goal.GoalController.from_snapshot(
            current_goal.evaluator, snapshot["goal"], current_goal.block_cap)
        root = Path(snapshot["workdir"]).resolve()
        if not root.is_dir():
            raise SessionError(f"会话启动目录不存在：{root}")
        if (scheduler.has_background_tasks() or subagent_runtime.list_subagents()
                or subagent_runtime.has_pending_inbox()):
            raise SessionError("仍有后台任务或结果未处理，请先完成再 /resume")
        if scheduler.has_pending_cron():
            raise SessionError("仍有计划任务结果未处理，请先完成再 /resume")
        assignment = tasks.validate_lead_context(target, snapshot, workdir=root)
        return snapshot, root, restored_goal, assignment

    # -- 落地阶段：切换事务的唯一真实写入点 --

    def _activate(self, snapshot, root, restored_goal, assignment):
        """按固定顺序写入全部归属，最后交还运行时刷新自身状态。"""
        core.WORKDIR = root
        self._store.workdir = root
        mcp_tools.mcp_clients.clear()
        self._replace_goal(restored_goal)
        with tasks.task_lock:
            self._lead_context.activate(snapshot["id"], snapshot)
            tasks.activate_lead_assignment(assignment)
            self._session.activate(snapshot)
        self._after_activate()

    # -- 对外唯一 interface --

    def switch(self, target):
        """切换当前会话；成功返回结果字典，失败保持当前状态并抛出 SessionError。"""
        snapshot, root, restored_goal, assignment = self._prepare(target)
        if not self._checkpoint():
            raise SessionError("当前会话保存失败，未切换上下文")
        directory_changed = root != core.WORKDIR.resolve()
        # 不在持 cron 锁时等待线程退出；服务停稳后重新检查一次队列。
        if directory_changed:
            scheduler.stop_runtime_services()
        try:
            with scheduler.require_idle_cron():
                if directory_changed:
                    os.chdir(root)
                self._activate(snapshot, root, restored_goal, assignment)
        except Exception as exc:
            if directory_changed and core.WORKDIR.resolve() != root:
                scheduler.start_runtime_services(load_jobs=False)
            if isinstance(exc, scheduler.PendingCronDeliveryError):
                raise SessionError(f"{exc}，请先完成再 /resume") from exc
            raise
        with interaction.execution_context(self._context()):
            try:
                self._record("resume", target, save=False)
                saved = self._checkpoint()
                if directory_changed:
                    scheduler.clear_runtime_jobs()
                    try:
                        subagent_runtime.consume_lead_inbox(route_protocol=False)
                    finally:
                        scheduler.start_runtime_services()
            except Exception as exc:
                # 不重复消费同一批已损坏的后台输入。
                self._pause_background()
                raise SessionError(f"已切换到会话 {target}（工作目录 {root}），但切换后的处理失败：{exc}") from exc
            finally:
                self._context().publish("session_changed",
                                        records=self._session.display_records(historical=True))
        text = f"已切换到会话 {target}（工作目录 {root}）"
        if not saved:
            text += "；上下文未能保存，将在下次交互时重试"
        return {"text": text, "workdir": str(root), "saved": saved}

    def start_new(self):
        """保存当前会话并在同一启动目录开启一个干净会话；失败保持当前会话。"""
        root = core.WORKDIR.resolve()
        if (scheduler.has_background_tasks() or subagent_runtime.list_subagents()
                or subagent_runtime.has_pending_inbox()):
            raise SessionError("仍有后台任务或结果未处理，请先完成再 /new")
        if scheduler.has_pending_cron():
            raise SessionError("仍有计划任务结果未处理，请先完成再 /new")
        if not self._checkpoint():
            raise SessionError("当前会话保存失败，未开启新会话")
        current_goal = self._read_goal()
        fresh_goal = goal.GoalController(current_goal.evaluator, current_goal.block_cap)
        snapshot = self._store.create()
        assignment = tasks.validate_lead_context(snapshot["id"], snapshot, workdir=root)
        try:
            with scheduler.require_idle_cron():
                self._activate(snapshot, root, fresh_goal, assignment)
        except scheduler.PendingCronDeliveryError as exc:
            raise SessionError(f"{exc}，请先完成再 /new") from exc
        with interaction.execution_context(self._context()):
            self._record("new_session", snapshot["id"], save=False)
            self._checkpoint()
            self._context().publish("session_changed",
                                    records=self._session.display_records(historical=True))
        return {"text": f"已开启新会话 {snapshot['id']}", "workdir": str(root), "saved": True}
