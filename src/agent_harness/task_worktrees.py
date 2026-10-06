"""将持久化任务绑定到 Git worktree，并移除已完成的绑定。"""

from __future__ import annotations

from pathlib import Path

from . import core
from . import scheduler
from . import tasks
from . import worktrees


def create_worktree(name: str, task_id: str) -> str:
    error = worktrees.validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    try:
        path = worktrees._worktree_path(name)
        task_path = tasks._task_path(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    branch = worktrees._worktree_branch(name)

    with tasks.task_store_lock():
        if not task_path.exists():
            return f"Error: Task {task_id} not found"
        task = tasks.load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            return f"Error: Task {task_id} must be pending and unowned"
        if task.worktree:
            return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
        if any(item.worktree == name for item in tasks.list_tasks()
               if item.id != task_id):
            return f"Error: Worktree '{name}' is already bound to another task"
        if path.exists():
            return f"Error: Worktree path already exists: {path}"

        ok, root = worktrees.run_git(["rev-parse", "--show-toplevel"])
        if not ok or Path(root).resolve() != core.WORKDIR.resolve():
            return "Error: Working directory must be the root of a Git repository"
        ok, branch_check = worktrees.run_git(["check-ref-format", "--branch", branch])
        if not ok:
            return f"Error: Invalid worktree branch '{branch}': {branch_check}"
        exists, _ = worktrees.run_git(["show-ref", "--verify", "--quiet",
                                       f"refs/heads/{branch}"])
        if exists:
            return f"Error: Branch '{branch}' already exists"
        entries, registry_error = worktrees._registered_worktrees()
        if registry_error:
            return f"Error: {registry_error}"
        if path in entries:
            return f"Error: Worktree path is already registered: {path}"

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
                return (
                    "Partial operation: git worktree add reported an error "
                    f"after leaving {', '.join(artifacts)}. Task {task_id} "
                    "remains unbound and no Git data was deleted. Run "
                    f"`git worktree list`, inspect '{path}' and '{branch}', "
                    "then keep or remove those artifacts manually after "
                    f"preserving any work. Git error: {result}"
                )
            return f"Git error: {result}"

        try:
            task.worktree = name
            tasks.save_task(task)
        except Exception as exc:
            return (f"Partial success: Worktree '{name}' was created at "
                    f"{path} on branch '{branch}', but task binding failed: "
                    f"{exc}. Git data was retained for manual recovery.")

    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    error = worktrees.validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    with tasks.task_store_lock():
        path, error = worktrees._registered_worktree(name)
        if error:
            return f"Error: {error}"
        bound = [task for task in tasks.list_tasks() if task.worktree == name]
        if not bound:
            return f"Error: Worktree '{name}' is not bound to a task"
        active = [task for task in bound if task.status != "completed"]
        if active:
            return (f"Error: Worktree '{name}' is bound to active task "
                    f"{active[0].id}; complete it before removal")
        leased = [owner for owner, assignment in tasks.teammate_assignments.items()
                  if Path(assignment["cwd"]).resolve() == path.resolve()]
        if leased:
            return (f"Error: Worktree '{name}' is still in use by "
                    f"{', '.join(sorted(leased))}; wait for the turn to end")
        with scheduler.background_lock:
            running = [item for item in scheduler.background_tasks.values()
                       if item.get("status") == "running"
                       and item.get("cwd")
                       and Path(item["cwd"]).resolve() == path.resolve()]
            if running:
                return (f"Error: Worktree '{name}' has a running background "
                        "command; wait for it to finish")

            ok, status = worktrees.run_git(
                ["status", "--porcelain", "--ignored"], cwd=path
            )
            if not ok:
                return f"Error: Cannot verify worktree '{name}' status: {status}"
            if status != "(no output)" and not discard_changes:
                changed = len([line for line in status.splitlines() if line.strip()])
                return (f"Error: Worktree '{name}' has {changed} uncommitted "
                        "change(s); preserve or discard them manually")

            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            args.append(str(path))
            ok, result = worktrees.run_git(args)
            if not ok:
                return f"Git error: {result}"

            try:
                for task in bound:
                    task.worktree = None
                    tasks.save_task(task)
            except Exception as exc:
                return (f"Partial success: Worktree '{name}' was removed and "
                        f"branch '{worktrees._worktree_branch(name)}' retained, "
                        f"but task unbinding failed: {exc}. Manual recovery is required.")

    print(f"  \033[33m[worktree] removed: {name}; branch retained\033[0m")
    return (f"Worktree '{name}' removed; branch "
            f"'{worktrees._worktree_branch(name)}' retained")
