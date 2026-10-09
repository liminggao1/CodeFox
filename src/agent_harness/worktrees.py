"""Git worktree 路径、注册信息校验和 Git 命令执行。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import core


def _worktrees_dir(workdir: Path | None = None) -> Path:
    """取得启动目录下存放独立 Git 工作目录的父目录。

    返回：
        ``core.WORKDIR / '.worktrees'``，这里只返回路径，不创建目录。
    """
    return (workdir or core.WORKDIR) / ".worktrees"


def _worktrees_root(workdir: Path | None = None) -> Path:
    """取得 worktree 父目录的真实路径，用于检查路径是否越界。

    返回：
        解析符号链接后的绝对路径 ``Path``。
    """
    return _worktrees_dir(workdir).resolve()


VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_worktree_name(name: str) -> str | None:
    """校验任务 worktree 工具接受的名称格式。

    输入：
        name: 以字母或数字开头、长度为 1-64 的名称，只允许字母、数字、
            点、下划线和短横线。
    返回：
        合法时返回 ``None``，否则返回错误字符串。
        名称中不能出现 ``..``；这里不检查目录或分支是否已被占用。
    """
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return ("worktree name must be 1-64 letters, digits, dots, "
                "underscores, or dashes, and start with a letter or digit")
    if name in {".", ".."} or ".." in name:
        return "worktree name cannot contain '..'"
    return None


def _worktree_path(name: str, workdir: Path | None = None) -> Path:
    """取得工作目录的真实路径，并确认它位于允许的 worktree 父目录内。

    输入：
        name: worktree 名称，名称格式由工具调用方另行校验。
    返回：
        ``.worktrees/<name>`` 的绝对路径，不创建目录。
    异常：
        ValueError: 路径越出允许范围，或指向 worktree 父目录本身。
    """
    root = (workdir or core.WORKDIR).resolve()
    worktree_root = _worktrees_root(root)
    path = (_worktrees_dir(root) / name).resolve()
    if (not worktree_root.is_relative_to(root)
            or not path.is_relative_to(worktree_root)
            or path == worktree_root):
        raise ValueError(f"Worktree path escapes directory: {name!r}")
    return path


def _worktree_branch(name: str) -> str:
    """生成任务 worktree 对应的分支名称。

    输入：
        name: worktree 名称。
    返回：
        ``wt/<name>`` 字符串；分支名是否合法由调用方通过 Git 检查。
    """
    return f"wt/{name}"


run_git_impl = None


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """执行 Git 并保留完整输出；测试时可使用注入的替代执行器。

    输入：
        args: Git 子命令和参数列表，不包含 ``git`` 可执行文件名。
        cwd: 命令工作目录，默认使用 ``core.WORKDIR``。
    返回：
        ``(是否成功, 输出文本)``。合并标准输出和错误输出并去掉首尾空白，
        无输出时使用 ``'(no output)'``。启动失败或超过配置的超时时间时返回失败及错误文本。
        创建、移除等子命令会实际改变 Git 状态。
    """
    if run_git_impl is not None:
        return run_git_impl(args, cwd)
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd or core.WORKDIR,
            capture_output=True, text=True, errors="replace", timeout=core.GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or "(no output)"


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """执行 Git，并限制返回给工具的输出长度。

    输入：
        args: Git 子命令和参数列表，不包含可执行文件名。
        cwd: 命令工作目录，默认使用 ``core.WORKDIR``。
    返回：
        ``_run_git`` 的 ``(是否成功, 输出文本)``，按 core.GIT_OUTPUT_CHAR_LIMIT 截断。
        解析 Git 注册表时使用未截断的 ``_run_git``。
    """
    ok, output = _run_git(args, cwd)
    return ok, output[:core.GIT_OUTPUT_CHAR_LIMIT]


def _registered_worktrees(workdir: Path | None = None) -> tuple[dict[Path, dict[str, str]], str | None]:
    """读取 Git 的 worktree 注册表，保留完整的 porcelain 格式输出。

    返回：
        ``(entries, error)``。entries 将解析后的目录路径映射到 Git 元数据，
        例如 ``branch`` 和 ``HEAD``。成功时 error 为 None；Git 执行失败时
        返回空字典和错误字符串。
    """
    ok, output = _run_git(["worktree", "list", "--porcelain"], cwd=workdir)
    if not ok:
        return {}, f"cannot read Git worktree registry: {output}"
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            raw_path = current.get("worktree")
            if raw_path:
                entries[Path(raw_path).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries, None


def _registered_worktree(name: str, *, workdir: Path | None = None) -> tuple[Path | None, str | None]:
    """校验工作目录的路径、Git 注册信息和对应分支。

    输入：
        name: worktree 名称，预期分支为 ``wt/<name>``。
    返回：
        目录存在、已注册且分支匹配时返回 ``(path, None)``，否则返回
        ``(None, 错误文本)``。任务归属和绑定关系由任务生命周期函数另行检查。
    """
    try:
        path = _worktree_path(name, workdir)
    except ValueError as exc:
        return None, str(exc)
    entries, error = _registered_worktrees(workdir)
    if error:
        return None, error
    if path not in entries:
        return None, f"worktree '{name}' is not registered with Git"
    if not path.is_dir():
        return None, f"worktree '{name}' is missing at {path}"
    expected_branch = f"refs/heads/{_worktree_branch(name)}"
    if entries[path].get("branch") != expected_branch:
        return None, (f"worktree '{name}' is not registered on expected "
                      f"branch '{_worktree_branch(name)}'")
    return path, None
