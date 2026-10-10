"""将持久化任务绑定到 Git worktree，并移除已完成的绑定。"""

from __future__ import annotations

from pathlib import Path

from .. import core
from ..runtime.lead_context import current_lead_context
from ..runtime import scheduler
from . import tasks
from . import worktrees


def create_worktree(name: str, task_id: str) -> str:
    """从当前 HEAD 创建独立 Git 工作目录，并绑定到尚未认领的任务。

    输入：
        name: 唯一 worktree 名称，用于目录 ``.worktrees/<name>`` 和新分支
            ``wt/<name>``。
        task_id: 已存在、状态为 pending、无 owner 且未绑定 worktree 的任务 ID。
    返回：
        成功确认字符串；拒绝、Git 错误或部分操作失败抛出 ValueError。
        成功时创建目录和分支，再保存 ``task.worktree``；全过程使用与任务认领
        相同的存储锁。部分失败时保留已产生的 Git 数据，供人工恢复。
    """
    error = worktrees.validate_worktree_name(name)
    if error:
        raise ValueError(f"Error: {error}")
    try:
        path = worktrees._worktree_path(name)
        task_path = tasks._task_path(task_id)
    except ValueError as exc:
        raise ValueError(f"Error: {exc}")
    branch = worktrees._worktree_branch(name)

    with tasks.task_store_lock():
        if not task_path.exists():
            raise ValueError(f"Error: Task {task_id} not found")
        task = tasks.load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError(f"Error: Task {task_id} must be pending and unowned")
        if task.worktree:
            raise ValueError(f"Error: Task {task_id} already uses worktree '{task.worktree}'")
        if any(item.worktree == name for item in tasks.list_tasks()
               if item.id != task_id):
            raise ValueError(f"Error: Worktree '{name}' is already bound to another task")
        if path.exists():
            raise ValueError(f"Error: Worktree path already exists: {path}")

        ok, root = worktrees.run_git(["rev-parse", "--show-toplevel"])
        if not ok or Path(root).resolve() != core.WORKDIR.resolve():
            raise ValueError("Error: Working directory must be the root of a Git repository")
        ok, branch_check = worktrees.run_git(["check-ref-format", "--branch", branch])
        if not ok:
            raise ValueError(f"Error: Invalid worktree branch '{branch}': {branch_check}")
        exists, _ = worktrees.run_git(["show-ref", "--verify", "--quiet",
                                       f"refs/heads/{branch}"])
        if exists:
            raise ValueError(f"Error: Branch '{branch}' already exists")
        entries, registry_error = worktrees._registered_worktrees()
        if registry_error:
            raise ValueError(f"Error: {registry_error}")
        if path in entries:
            raise ValueError(f"Error: Worktree path is already registered: {path}")

        worktrees._worktrees_dir().mkdir(parents=True, exist_ok=True)
        ok, result = worktrees.run_git(["worktree", "add", "-b", branch,
                                        str(path), "HEAD"])
        if not ok:
            entries, registry_error = worktrees._registered_worktrees()
            branch_exists, _ = worktrees.run_git(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            )
            artifacts = []
            if path.exists():
                artifacts.append(f"checkout path '{path}'")
            if registry_error is None and path in entries:
                artifacts.append("registered Git worktree")
            if branch_exists:
                artifacts.append(f"branch '{branch}'")
            if artifacts:
                raise ValueError("Partial operation: git worktree add reported an error "
                    f"after leaving {', '.join(artifacts)}. Task {task_id} "
                    "remains unbound and no Git data was deleted. Run "
                    f"`git worktree list`, inspect '{path}' and '{branch}', "
                    "then keep or remove those artifacts manually after "
                    f"preserving any work. Git error: {result}")
            raise ValueError(f"Git error: {result}")

        try:
            task.worktree = name
            tasks.save_task(task)
        except Exception as exc:
            raise ValueError(f"Partial success: Worktree '{name}' was created at "
                    f"{path} on branch '{branch}', but task binding failed: "
                    f"{exc}. Git data was retained for manual recovery.")

    core.terminal_print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """移除已完成任务的工作目录，成功后释放 Lead 自己的占用并解除绑定。

    输入：
        name: 已注册且绑定了任务的 worktree 名称。
        discard_changes: 是否允许 Git 丢弃未提交文件，包括未跟踪和被忽略的文件。
            默认为 False，目录存在这些文件或改动时拒绝移除。
    返回：
        成功确认字符串；拒绝、Git 错误或部分操作失败抛出 ValueError。
        要求绑定任务均已完成、没有子 agent占用，且目录内没有运行中的后台命令。
        Lead 自己匹配的已完成分配可随清理释放；拒绝或 Git 失败时保留该分配。
        成功时删除目录并清空 ``task.worktree``，保留分支和任务记录。
        Git 已移除目录但保存解绑信息失败时，异常说明已经发生的目录变化。
    """
    error = worktrees.validate_worktree_name(name)
    if error:
        raise ValueError(f"Error: {error}")
    with tasks.task_store_lock():
        path, error = worktrees._registered_worktree(name)
        if error:
            raise ValueError(f"Error: {error}")
        bound = [task for task in tasks.list_tasks() if task.worktree == name]
        if not bound:
            raise ValueError(f"Error: Worktree '{name}' is not bound to a task")
        active = [task for task in bound if task.status != "completed"]
        if active:
            raise ValueError(f"Error: Worktree '{name}' is bound to active task "
                    f"{active[0].id}; complete it before removal")
        leased = [owner for owner, assignment in tasks.assignments.items()
                  if Path(assignment["cwd"]).resolve() == path.resolve()]
        lead_assignment = tasks.assignments.get("agent")
        release_lead = "agent" in leased and any(
            task.owner == "agent" and task.id == lead_assignment["task_id"]
            for task in bound)
        if release_lead:
            leased.remove("agent")
        if leased:
            raise ValueError(f"Error: Worktree '{name}' is still in use by "
                    f"{', '.join(sorted(leased))}; wait for the turn to end")
        with scheduler.background_lock:
            running = [item for item in scheduler.background_tasks.values()
                       if item.get("status") == "running"
                       and item.get("cwd")
                       and Path(item["cwd"]).resolve() == path.resolve()]
            if running:
                raise ValueError(f"Error: Worktree '{name}' has a running background "
                        "command; wait for it to finish")

            ok, status = worktrees.run_git(
                ["status", "--porcelain", "--ignored"], cwd=path
            )
            if not ok:
                raise ValueError(f"Error: Cannot verify worktree '{name}' status: {status}")
            if status != "(no output)" and not discard_changes:
                changed = len([line for line in status.splitlines() if line.strip()])
                raise ValueError(f"Error: Worktree '{name}' has {changed} uncommitted "
                        "change(s); preserve or discard them manually")

            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            args.append(str(path))
            ok, result = worktrees.run_git(args)
            if not ok:
                raise ValueError(f"Git error: {result}")

            try:
                # 只有 Git 已移除目录后才结束 Lead 的分配，失败时保留原 cwd。
                current_lead_context().unbind_worktree(name)
                if release_lead:
                    tasks.release_completed_assignment("agent")
                for task in bound:
                    task.worktree = None
                    tasks.save_task(task)
            except Exception as exc:
                raise ValueError(f"Partial success: Worktree '{name}' was removed and "
                        f"branch '{worktrees._worktree_branch(name)}' retained, "
                        f"but task cleanup failed: {exc}. Manual recovery is required.")

    core.terminal_print(f"  \033[33m[worktree] removed: {name}; branch retained\033[0m")
    result = (f"Worktree '{name}' removed; branch "
              f"'{worktrees._worktree_branch(name)}' retained")
    if release_lead:
        result += f"; Lead assignment released; default work directory: {core.WORKDIR}"
    return result
