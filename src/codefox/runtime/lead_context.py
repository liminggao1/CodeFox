"""Lead 会话状态的唯一拥有者；任务分配仍由任务板维护。"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy


def validate_todos(todos):
    """检查清单结构，不修复参数或判断任务是否完成。"""
    if not isinstance(todos, list):
        raise ValueError("Error: todos must be a list")
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            raise ValueError(f"Error: todos[{i}] must be an object")
        if "content" not in todo or "status" not in todo:
            raise ValueError(f"Error: todos[{i}] missing 'content' or 'status'")
        if not isinstance(todo["content"], str):
            raise ValueError(f"Error: todos[{i}].content must be a string")
        if todo["status"] not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Error: todos[{i}] has invalid status '{todo['status']}'")


class LeadContext:
    def __init__(self, lock):
        # 运行时显式注入任务板的 RLock，关联更新不增加第二把状态锁。
        self._lock = lock
        self._session_id = None
        self._worktree = None
        self._todos = []

    @property
    def session_id(self):
        with self._lock:
            return self._session_id

    @property
    def worktree(self):
        with self._lock:
            return self._worktree

    @property
    def todos(self):
        with self._lock:
            return deepcopy(self._todos)

    def activate(self, session_id, context):
        validate_todos(context["todos"])
        with self._lock:
            self._session_id = session_id
            self._worktree = context["worktree"]
            self._todos = deepcopy(context["todos"])

    def replace_todos(self, todos):
        validate_todos(todos)
        with self._lock:
            self._todos = deepcopy(todos)
            return deepcopy(self._todos)

    def bind_worktree(self, name):
        with self._lock:
            self._worktree = name

    def unbind_worktree(self, name):
        with self._lock:
            if self._worktree == name:
                self._worktree = None

    def snapshot(self, task_id=None):
        with self._lock:
            return {"task_id": task_id, "worktree": self._worktree,
                    "todos": deepcopy(self._todos)}


_offline_context = ContextVar("offline_lead_context", default=None)


@contextmanager
def lead_context_scope(context):
    """离线入口显式提供状态；不创建交互通道或启用模型流式调用。"""
    token = _offline_context.set(context)
    try:
        yield context
    finally:
        _offline_context.reset(token)


def current_lead_context():
    from .interaction import current_context
    execution = current_context()
    context = execution.lead_context if execution is not None else _offline_context.get()
    if context is None:
        raise RuntimeError("LeadContext is required; provide it through runtime or lead_context_scope")
    return context
