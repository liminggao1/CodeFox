"""持久化任务板：记录归属和依赖关系的文件存储。

任务是启动目录 .tasks/ 下的小型 JSON 记录。worktree 和子 agent复用该存储；
Approval state belongs to agents/subagent_protocol.py.
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

from .. import core
from ..runtime.lead_context import current_lead_context
from . import worktrees

fcntl = core.fcntl

# -- 任务系统 --
TASK_ID_ALLOCATION_ATTEMPTS = 100

# 任务是小型持久化记录；归属、依赖、worktree 和子 agent都建立在同一份文件状态上。
def _tasks_dir(workdir: Path | None = None) -> Path:
    """取得启动目录下的任务存储目录。

    返回：
        ``core.WORKDIR / '.tasks'``，这里只返回路径，不创建目录。
    """
    return (workdir or core.WORKDIR) / ".tasks"

def _tasks_root(workdir: Path | None = None) -> Path:
    """取得任务存储目录的真实路径，用于检查路径是否越界。

    返回：
        解析符号链接后的绝对路径 ``Path``。
    """
    return _tasks_dir(workdir).resolve()


TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
task_lock = threading.RLock()
_task_store_state = threading.local()

# owner -> {"task_id": str, "cwd": Path}。每个子 agent一次只领取一个任务，
# 文件系统工具都通过该映射确定 cwd。
assignments: dict[str, dict[str, object]] = {}
assignment_versions: dict[str, int] = {}


def assignment_identity(owner: str) -> tuple[int, str | None]:
    """Read the current assignment version and task ID together under its lock."""
    with task_lock:
        assignment = assignments.get(owner)
        task_id = str(assignment["task_id"]) if assignment else None
        return assignment_versions.get(owner, 0), task_id


def active_lead_task_id():
    """只读当前 Lead 分配，不修改会话关联或保存。"""
    with task_lock:
        assignment = assignments.get("agent")
        task = load_task(str(assignment["task_id"])) if assignment else None
        return task.id if task and task.status == "in_progress" else None


def validate_lead_context(session_id: str, context: dict, *, workdir: Path) -> dict | None:
    """Validate without changing the current assignment or session directory."""
    with task_lock:
        cwd = workdir
        name = context["worktree"]
        if name is not None:
            cwd, error = worktrees._registered_worktree(name, workdir=workdir)
            if error:
                raise ValueError(error)
        if context["task_id"] is None:
            return None
        task = load_task(context["task_id"], workdir=workdir)
        if task.owner != "agent" or task.session_id != session_id:
            raise ValueError(f"Task {task.id} belongs to another owner or session")
        if task.status not in {"in_progress", "completed"}:
            raise ValueError(f"Task {task.id} is no longer active")
        if task.worktree != name:
            raise ValueError(f"Worktree binding changed for task {task.id}")
        return {"task_id": task.id, "cwd": cwd} if task.status == "in_progress" else None


def activate_lead_assignment(assignment=None):
    """显式替换 Lead 分配；会话状态由运行时单独激活。"""
    with task_lock:
        assignments.pop("agent", None)
        if assignment is not None:
            assignments["agent"] = dict(assignment)
        advance_assignment_version("agent")


@contextmanager
def task_store_lock():
    """让任务写入、认领和 worktree 绑定串行执行，支持同一线程嵌套加锁。

    输出：
        供 ``with task_store_lock():`` 使用的上下文管理器。
        进入时持有进程内线程锁；POSIX 下还持有跨进程的 ``.tasks/.lock``
        文件锁。嵌套调用复用文件锁，最外层退出时释放；按需创建目录和锁文件。
    """
    with task_lock:
        depth = getattr(_task_store_state, "depth", 0)
        if depth == 0:
            _tasks_dir().mkdir(parents=True, exist_ok=True)
            handle = (_tasks_dir() / ".lock").open("a+", encoding="utf-8")
            # POSIX 上用 fcntl.flock 跨进程加锁；Windows 没有 fcntl，
            # 因此只使用进程内的线程锁。
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
    """递增执行者的任务分配版本，用于识别已经过期的协议回复。

    输入：
        owner: 主 agent 的身份 ``'agent'``，或子 agent名称。
    返回：
        ``None``。将内存中的 ``assignment_versions[owner]`` 加一。
    """
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
    session_id: str | None = None


def _task_path(task_id: str, workdir: Path | None = None) -> Path:
    """校验任务 ID，并取得任务存储目录内对应的 JSON 文件路径。

    输入：
        task_id: 格式为 ``task_`` 加 8 位小写十六进制字符的运行时 ID。
    返回：
        任务文件的绝对路径，不检查文件是否存在。
    异常：
        ValueError: ID 格式不合法，或解析后的路径越出允许范围。
    """
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"Invalid task ID: {task_id!r}")
    root = (workdir or core.WORKDIR).resolve()
    task_root = _tasks_root(root)
    path = (_tasks_dir(root) / f"{task_id}.json").resolve()
    if (not task_root.is_relative_to(root)
            or not path.is_relative_to(task_root)):
        raise ValueError(f"Invalid task ID: {task_id!r}")
    return path


def create_task(subject: str, description: str = "") -> Task:
    """生成唯一任务 ID，创建并保存一个尚未认领的任务。

    输入：
        subject: 任务标题，会去掉首尾空白。
        description: 可选的任务详情。
    返回：
        已保存的 ``Task``，初始状态为 ``pending``，无 owner、依赖或 worktree。
        在任务存储锁内写入 ``.tasks/<id>.json``。
    异常：
        ValueError: 去掉空白后的标题为空。
        RuntimeError: 尝试 100 次后仍无法生成未占用的 ID。
    """
    subject = subject.strip()
    if not subject:
        raise ValueError("Task subject cannot be empty")
    with task_store_lock():
        for _ in range(TASK_ID_ALLOCATION_ATTEMPTS):
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
    """沿任务依赖关系查找目标，用于判断是否会形成循环依赖。

    输入：
        task_id: 开始查找的任务 ID。
        target_id: 要查找的目标 ID，起始任务本身也纳入判断。
    返回：
        能到达目标时为 ``True``，否则为 ``False``。
        读取任务记录，并记录已访问的 ID，避免重复遍历。
    """
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
    """为尚未认领的待办任务追加依赖，并保存更新后的记录。

    输入：
        task_id: 要更新的任务 ID。
        addBlockedBy: 必须先完成的现有任务 ID 列表。
    返回：
        已保存的 ``Task``，新依赖追加到原列表中，重复 ID 不会重复加入。
    异常：
        ValueError: 任务已被认领或不是待办状态、输入不是列表、
            依赖不存在，或新增依赖会形成循环。
    """
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
    """在任务存储锁内，将任务的当前字段原子地写入 JSON 文件。

    输入：
        task: 要保存的任务对象。
    返回：
        ``None``。先写临时文件，再替换 ``.tasks/<id>.json``，最后清理临时文件。
        文件系统错误会继续抛给调用方。
    """
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


def load_task(task_id: str, *, workdir: Path | None = None) -> Task:
    """从 JSON 文件读取任务，并校验记录中的 ID 和生命周期状态。

    输入：
        task_id: 要读取的运行时任务 ID。
    返回：
        ID 与请求一致、状态合法的 ``Task`` 对象。
    异常：
        FileNotFoundError: 任务文件不存在。
        ValueError: ID、JSON 数据或记录中的状态不合法。
    """
    with task_lock:
        data = json.loads(_task_path(task_id, workdir).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in {"pending", "in_progress", "completed"}:
            raise ValueError(f"Invalid task status: {task.status}")
        return task


def list_tasks() -> list[Task]:
    """在线程锁内按文件名顺序读取任务存储中的全部任务。

    返回：
        校验后的 ``Task`` 对象列表；存储目录不存在时返回 ``[]``。
        某条记录无法读取或校验失败时，继续抛出 ``load_task`` 的异常。
    """
    with task_lock:
        if not _tasks_dir().exists():
            return []
        if not _tasks_root().is_relative_to(core.WORKDIR.resolve()):
            raise ValueError("Tasks directory escapes workspace")
        return [load_task(path.stem)
                for path in sorted(_tasks_dir().glob("task_*.json"))]


def get_task_json(task_id: str) -> str:
    """将校验后的任务记录转换为工具可返回的格式化 JSON。

    输入：
        task_id: 要读取的任务 ID。
    返回：
        包含任务全部字段的 JSON 字符串；读取异常会继续抛出。
    """
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    """检查任务的前置依赖是否完成，不认领任务，也不改变任务状态。

    输入：
        task_id: 要检查依赖的任务 ID。
    返回：
        所有依赖均存在且已完成时返回 ``True``，没有依赖也返回 ``True``。
        依赖 ID 不合法、依赖缺失或未完成时返回 ``False``。
        不检查当前任务的状态和 owner，这些由 ``claim_task`` 校验。
    """
    # 依赖规则保持简单：阻塞任务必须存在且已完成，当前任务才能被领取。
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
    """从持久化任务中查找执行者正在处理的第一个任务。

    输入：
        owner: 主 agent 的身份 ``'agent'``，或子 agent名称。
    返回：
        属于该执行者且状态为 ``in_progress`` 的 ``Task``；没有则返回 ``None``。
    """
    return next((task for task in list_tasks()
                 if task.status == "in_progress" and task.owner == owner
                 and (owner != "agent" or task.session_id == current_lead_context().session_id)), None)


def _incomplete_dependencies(task: Task) -> list[str]:
    """收集阻止任务执行的依赖 ID，供认领失败时说明原因。

    输入：
        task: 要检查 ``blockedBy`` 列表的任务。
    返回：
        不合法、缺失或未完成的依赖 ID 列表，保留原有顺序。
    """
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
    """认领满足依赖条件的任务，并登记执行者使用的工具工作目录。

    输入：
        task_id: 待办、无 owner 且依赖已完成的任务 ID。
        owner: 主 agent 的身份 ``'agent'``，或认领任务的子 agent名称。
    返回：
        认领成功的字符串；拒绝抛出 ValueError。成功时在存储锁内保存 ``in_progress``
        和 owner，将任务目录登记到 ``assignments``，并递增分配版本。
        ID 不合法、记录缺失或保存失败时，异常会继续抛出。
    """
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "pending":
            raise ValueError(f"Task {task_id} is {task.status}, cannot claim")
        if task.owner or task.session_id:
            raise ValueError(f"Task {task_id} is already owned by {task.owner}")
        assignment = assignments.get(owner)
        if assignment:
            raise ValueError(f"Owner {owner} must finish the current work turn for "
                    f"{assignment['task_id']} before claiming another task")
        current = _owner_in_progress(owner)
        if current:
            raise ValueError(f"Owner {owner} must complete {current.id} before "
                    "claiming another task")
        if not can_start(task_id):
            raise ValueError(f"Blocked by: {_incomplete_dependencies(task)}")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(f"Cannot claim {task_id}: {error}")
        task.owner = owner
        task.status = "in_progress"
        if owner == "agent":
            task.session_id = current_lead_context().session_id
        save_task(task)
        assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)
        if owner == "agent":
            current_lead_context().bind_worktree(task.worktree)
    core.terminal_print(f"  \033[36m[claim] {task.subject} -> in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    """由认领者将正在处理的任务标记为完成。

    输入：
        task_id: 要完成的任务 ID。
        owner: 必须与任务记录中的认领者一致。
    返回：
        完成成功的字符串；拒绝抛出 ValueError。成功时附带已满足依赖条件的任务标题。
        保存 ``completed``，保留任务文件和分配目录；回合结束或成功移除 worktree 后释放分配。
        这里只记录完成状态，不独立验收工作成果。
        ID 不合法、记录缺失或保存失败时，异常会继续抛出。
    """
    with task_store_lock():
        task = load_task(task_id)
        if task.status != "in_progress":
            raise ValueError(f"Task {task_id} is {task.status}, cannot complete")
        if task.owner != owner:
            raise ValueError(f"Task {task_id} is owned by {task.owner}, "
                    f"not {owner}; cannot complete")
        if owner == "agent" and task.session_id != current_lead_context().session_id:
            raise ValueError(f"Task {task_id} belongs to another session; cannot complete")
        assignment = assignments.get(owner)
        if not assignment or assignment.get("task_id") != task.id:
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(f"Task {task_id} cannot complete: {error}")
            assignments[owner] = {"task_id": task.id, "cwd": cwd}
            if owner == "agent":
                current_lead_context().bind_worktree(task.worktree)
        task.status = "completed"
        save_task(task)
        unblocked = [t.subject for t in list_tasks()
                     if t.status == "pending" and t.blockedBy and can_start(t.id)]
    core.terminal_print(f"  \033[32m[complete] {task.subject}\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        core.terminal_print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


def task_worktree_cwd(task: Task) -> tuple[Path, str | None]:
    """取得任务的文件工具应使用的工作目录。

    输入：
        task: 可能绑定了 worktree 名称的任务。
    返回：
        ``(cwd, error)``。未绑定时返回 ``(core.WORKDIR, None)``；绑定有效时
        返回对应 Git 工作目录。绑定校验失败时路径仍为 ``core.WORKDIR``，
        但同时带有错误，调用方必须先处理错误，不能直接在这个目录执行任务。
    """
    if not task.worktree:
        return core.WORKDIR, None
    path, error = worktrees._registered_worktree(task.worktree)
    return (path or core.WORKDIR), error


def assignment_cwd(owner: str) -> Path:
    """取得执行者的任务目录，必要时从任务记录恢复内存中的分配信息。

    输入：
        owner: 主 agent 的身份 ``'agent'``，或子 agent名称。
    返回：
        正在执行或尚未释放的已完成任务的有效目录。
        无分配且无正在执行的任务时返回 ``core.WORKDIR``。
        可能根据持久化记录补回 ``assignments``。
    异常：
        ValueError: owner、任务状态或绑定目录不合法，或目录发生变化。
        任务文件缺失等读取异常也会继续抛出。
    """
    with task_lock:
        assignment = assignments.get(owner)
        task = _owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            assignments[owner] = assignment
            if owner == "agent":
                current_lead_context().bind_worktree(task.worktree)
        elif not assignment:
            if owner == "agent" and current_lead_context().worktree is not None:
                cwd, error = worktrees._registered_worktree(current_lead_context().worktree)
                if error:
                    raise ValueError(error)
                return cwd
            return core.WORKDIR
        task = load_task(str(assignment["task_id"]))
        if task.status not in {"in_progress", "completed"} or task.owner != owner:
            raise ValueError(f"Assignment for {owner} is no longer active")
        if owner == "agent" and task.session_id != current_lead_context().session_id:
            raise ValueError(f"Assignment for {owner} belongs to another session")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            raise ValueError(f"Assignment cwd changed for task {task.id}")
        return cwd


def release_completed_assignment(owner: str) -> bool:
    """在回合结束或 worktree 清理成功后释放执行者已完成任务的分配记录。

    输入：
        owner: 主 agent 的身份 ``'agent'``，或子 agent名称。
    返回：
        成功移除匹配的已完成分配并递增版本时返回 ``True``，否则返回 ``False``。
        保留已完成任务的文件、owner 字段和 worktree 绑定。
    """
    with task_lock:
        assignment = assignments.get(owner)
        if not assignment:
            return False
        task = load_task(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False
        assignments.pop(owner, None)
        advance_assignment_version(owner)
        return True


def release_assignment(owner: str):
    """在子 agent退出时释放分配，让未完成任务可以再次被认领。

    输入：
        owner: 要释放分配的子 agent名称。
    返回：
        ``None``。将正在处理的任务重置为无 owner 的 ``pending``，保留 worktree。
        即使读取或保存任务失败，也会清理内存分配并递增版本；异常仍会抛出。
    """
    with task_store_lock():
        try:
            task = _owner_in_progress(owner)
            if task:
                task.status = "pending"
                task.owner = None
                task.session_id = None
                save_task(task)
        finally:
            assignments.pop(owner, None)
            advance_assignment_version(owner)
