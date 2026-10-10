"""本地命令路由：解析 /quit、/goal、/resume、/delete 并返回明确结果。

命令结果通过 ``CommandOutcome`` 显式返回，不再写运行时实例字段；
``/goal <text>`` 需要后续回合时，用 ``followup`` 表达，而不是隐藏通道。
会话切换仍由 [session_switch](session_switch.py) 独占。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agents import goal
from .session_store import SessionError


@dataclass
class CommandOutcome:
    """一条本地命令的结果：命令名、结果数据，以及可选的后续回合输入。"""

    command: str
    data: dict = field(default_factory=dict)
    followup: str | None = None


class CommandRouter:
    """把命令分派依赖显式注入；不持有运行时状态。"""

    def __init__(self, *, store, session_id, read_goal, record, close, session_switch):
        self._store = store
        self._session_id = session_id
        self._read_goal = read_goal
        self._record = record
        self._close = close
        self._switch = session_switch

    def dispatch(self, text: str) -> CommandOutcome:
        """执行一条本地命令；参数错误按既有文案抛出 SessionError。"""
        parts = text.split()
        command = parts[0]
        if command == "/quit":
            self._close()
            return CommandOutcome(command)
        if command == "/goal":
            return self._goal(text)
        if command == "/resume" and len(parts) == 1:
            entries, errors = self._store.list_sessions()
            return CommandOutcome(command, {"sessions": entries, "errors": errors})
        if len(parts) != 2:
            raise SessionError(f"Usage: {command} <session_id>")
        target = parts[1]
        self._store.path(target)
        if command == "/delete":
            if target == self._session_id():
                raise SessionError("Cannot delete the active session")
            self._store.delete(target)
            return CommandOutcome(command, {"text": f"Deleted {target}"})
        if target == self._session_id():
            return CommandOutcome(command, {"text": f"Already using {target}"})
        return CommandOutcome(command, self._switch.switch(target))

    def _goal(self, text: str) -> CommandOutcome:
        """设置、清除或查询目标；设置目标后仍需要跑一个回合。"""
        argument = text[5:].strip()
        controller = self._read_goal()
        if not argument:
            self._record("goal_query", text, save=False)
            return CommandOutcome("/goal", {"text": controller.status(0)})
        if argument.lower() in goal.CLEAR_ALIASES:
            self._record("goal_clear", text, save=False)
            return CommandOutcome("/goal", {"text": controller.clear()})
        controller.set_goal(argument)
        self._record("goal_set", argument, save=False)
        return CommandOutcome("/goal", {"text": f"Goal set: {argument}"}, followup=argument)