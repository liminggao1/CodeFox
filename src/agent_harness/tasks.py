"""Durable task board: file-backed records with ownership and dependencies.

Tasks are tiny JSON records under .tasks/ in the launch directory. Worktrees
and teammates build on this same store; approval state belongs to teams.py.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path

from . import core
from . import worktrees

fcntl = core.fcntl

# -- Task System --

# Tasks are tiny durable records. Later systems add ownership, dependencies,
# worktrees, and teammates on top of this same file-backed state.
def _tasks_dir() -> Path:
    return core.WORKDIR / ".tasks"

def _tasks_root() -> Path:
    return _tasks_dir().resolve()


TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
task_lock = threading.RLock()
_task_store_state = threading.local()
CURRENT_TODOS: list[dict] = []

# owner -> {"task_id": str, "cwd": Path}. A teammate gets one assignment at
# a time, and every filesystem tool resolves its cwd through this registry.
teammate_assignments: dict[str, dict[str, object]] = {}
assignment_versions: dict[str, int] = {}


@contextmanager
def task_store_lock():
    """Serialize task mutations across threads and host processes."""
    with task_lock:
        depth = getattr(_task_store_state, "depth", 0)
        if depth == 0:
            _tasks_dir().mkdir(parents=True, exist_ok=True)
            handle = (_tasks_dir() / ".lock").open("a+", encoding="utf-8")
            # fcntl.flock gives cross-process locking on POSIX. Windows has no
            # fcntl, so it falls back to the in-process threading lock only.
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _task_store_state.handle = handle
        _task_store_state.depth = depth + 1
        try:
            yield
        finally:
            _task_store_state.depth -= 1
            if _task_store_state.depth == 0:
                handle = _task_store_state.handle
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                del _task_store_state.handle


def advance_assignment_version(owner: str):
    with task_lock:
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None


def _task_path(task_id: str) -> Path:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"Invalid task ID: {task_id!r}")
    path = (_tasks_dir() / f"{task_id}.json").resolve()
    if (not _tasks_root().is_relative_to(core.WORKDIR.resolve())
            or not path.is_relative_to(_tasks_root())):
        raise ValueError(f"Invalid task ID: {task_id!r}")
    return path


def create_task(subject: str, description: str = "") -> Task:
    subject = subject.strip()
    if not subject:
        raise ValueError("Task subject cannot be empty")
    with task_store_lock():
        for _ in range(100):
            task = Task(
                id=f"task_{secrets.token_hex(4)}",
                subject=subject,
                description=description,
                status="pending",
                owner=None,
                blockedBy=[],
            )
            try:
                with _task_path(task.id).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, indent=2)
                return task
            except FileExistsError:
                continue
    raise RuntimeError("Could not allocate a unique task ID")


def _task_depends_on(task_id: str, target_id: str) -> bool:
    """Return whether task_id transitively depends on target_id."""
    pending = [task_id]
    visited = set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(load_task(current).blockedBy)
    return False


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    """Add dependency edges after create_task has returned real task IDs."""
    if not isinstance(addBlockedBy, list):
        raise ValueError("addBlockedBy must be a list of task IDs")

    with task_store_lock():
        task = load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError(
                f"Task {task_id} dependencies can only be updated while "
                "pending and unowned"
            )

        dependencies = list(dict.fromkeys(addBlockedBy))
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not _task_path(dependency).is_file():
                raise ValueError(f"Dependency not found: {dependency}")
            if dependency not in task.blockedBy and _task_depends_on(
                dependency, task_id
            ):
                raise ValueError(
                    f"Dependency cycle detected: {task_id} -> {dependency}"
                )

        task.blockedBy.extend(
            dependency for dependency in dependencies
            if dependency not in task.blockedBy
        )
        save_task(task)
        return task


def save_task(task: Task):
    with task_store_lock():
        path = _task_path(task.id)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(asdict(task), indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def load_task(task_id: str) -> Task:
    with task_lock:
        data = json.loads(_task_path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in {"pending", "in_progress", "completed"}:
            raise ValueError(f"Invalid task status: {task.status}")
        return task


def list_tasks() -> list[Task]:
    with task_lock:
        if not _tasks_dir().exists():
            return []
        if not _tasks_root().is_relative_to(core.WORKDIR.resolve()):
            raise ValueError("Tasks directory escapes workspace")
        return [load_task(path.stem)
                for path in sorted(_tasks_dir().glob("task_*.json"))]


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    # Dependencies are intentionally simple: every blocker must exist and be
    # completed before the task can be claimed.
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            return False
        if not dep_path.exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def _owner_in_progress(owner: str) -> Task | None:
    return next((task for task in list_tasks()
                 if task.status == "in_progress" and task.owner == owner), None)


def _incomplete_dependencies(task: Task) -> list[str]:
    incomplete = []
    for dep_id in task.blockedBy:
        try:
            dep_path = _task_path(dep_id)
        except ValueError:
            incomplete.append(dep_id)
            continue
        if not dep_path.exists() or load_task(dep_id).status != "completed":
            incomplete.append(dep_id)
    return incomplete


def claim_task(task_id: str, owner: str = "agent") -> str:
    """Atomically claim one task and bind the owner's filesystem cwd."""
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} is already owned by {task.owner}"
        assignment = teammate_assignments.get(owner)
        if assignment:
            return (f"Owner {owner} must finish the current work turn for "
                    f"{assignment['task_id']} before claiming another task")
        current = _owner_in_progress(owner)
        if current:
            return (f"Owner {owner} must complete {current.id} before "
                    "claiming another task")
        if not can_start(task_id):
            return f"Blocked by: {_incomplete_dependencies(task)}"
        cwd, error = task_worktree_cwd(task)
        if error:
            return f"Cannot claim {task_id}: {error}"
        task.owner = owner
        task.status = "in_progress"
        save_task(task)
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)
    print(f"  \033[36m[claim] {task.subject} -> in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    """Complete an assignment only when the caller owns it."""
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        if task.owner != owner:
            return (f"Task {task_id} is owned by {task.owner}, "
                    f"not {owner}; cannot complete")
        assignment = teammate_assignments.get(owner)
        if not assignment or assignment.get("task_id") != task.id:
            cwd, error = task_worktree_cwd(task)
            if error:
                return f"Task {task_id} cannot complete: {error}"
            teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        task.status = "completed"
        save_task(task)
        unblocked = [t.subject for t in list_tasks()
                     if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject}\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


def task_worktree_cwd(task: Task) -> tuple[Path, str | None]:
    if not task.worktree:
        return core.WORKDIR, None
    path, error = worktrees._registered_worktree(task.worktree)
    return (path or core.WORKDIR), error


def assignment_cwd(owner: str) -> Path:
    with task_lock:
        assignment = teammate_assignments.get(owner)
        task = _owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            teammate_assignments[owner] = assignment
        elif not assignment:
            return core.WORKDIR
        task = load_task(str(assignment["task_id"]))
        if task.status not in {"in_progress", "completed"} or task.owner != owner:
            raise ValueError(f"Assignment for {owner} is no longer active")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            raise ValueError(f"Assignment cwd changed for task {task.id}")
        return cwd


def release_completed_assignment(owner: str) -> bool:
    with task_lock:
        assignment = teammate_assignments.get(owner)
        if not assignment:
            return False
        task = load_task(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        return True


def release_teammate_assignment(owner: str):
    with task_store_lock():
        try:
            task = _owner_in_progress(owner)
            if task:
                task.status = "pending"
                task.owner = None
                save_task(task)
        finally:
            teammate_assignments.pop(owner, None)
            advance_assignment_version(owner)
