"""读取当前 Git 工作区变更，供本地 Web 工作台展示。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import worktrees


MAX_FILE_BYTES = 512 * 1024
MAX_PATCH_CHARS = 200_000
MAX_TOTAL_PATCH_CHARS = 2_000_000


class DiffError(Exception):
    """读取工作区 Diff 时的结构化错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StatusEntry:
    """一个 Git 状态条目的前端展示信息。"""

    path: str
    status: str
    previous_path: str | None = None


def _run_git(args: list[str], root: Path) -> str:
    """运行只读 Git 查询，失败时转换成工作区接口使用的错误。"""
    ok, output = worktrees.run_git_full(args, root)
    if not ok:
        raise DiffError("git_command_failed", output)
    return output


def _git_root(workdir: Path) -> Path:
    """解析工作目录对应的 Git 根目录，拒绝非仓库目录。"""
    try:
        current = workdir.resolve()
    except OSError as exc:
        raise DiffError("invalid_workdir", f"无法读取工作目录：{exc}") from exc
    ok, output = worktrees.run_git_full(["rev-parse", "--show-toplevel"], current)
    if not ok:
        raise DiffError("not_git_repository", f"当前目录不是 Git 仓库：{current}")
    try:
        return Path(output.strip()).resolve()
    except OSError as exc:
        raise DiffError("invalid_git_root", f"无法解析 Git 根目录：{exc}") from exc


def _branch(root: Path) -> str:
    """取得当前分支名；分离头指针和无提交仓库使用明确文本。"""
    ok, output = worktrees.run_git_full(["symbolic-ref", "--short", "-q", "HEAD"], root)
    if ok and output.strip():
        return output.strip()
    ok, output = worktrees.run_git_full(["rev-parse", "--short", "HEAD"], root)
    return f"HEAD detached at {output.strip()}" if ok else "未提交"


def _commit(root: Path) -> str | None:
    """取得当前提交；空仓库没有提交时返回空值。"""
    ok, output = worktrees.run_git_full(["rev-parse", "HEAD"], root)
    return output.strip() if ok and output.strip() else None


def _status_name(index_status: str, worktree_status: str) -> str:
    """把 Git 的双列状态转换成稳定的展示名称。"""
    statuses = index_status + worktree_status
    if "?" in statuses:
        return "untracked"
    if "R" in statuses:
        return "renamed"
    if "C" in statuses:
        return "copied"
    if "D" in statuses:
        return "deleted"
    if "A" in statuses:
        return "added"
    return "modified"


def _parse_status(raw: str) -> list[StatusEntry]:
    """解析 porcelain v1 的 NUL 分隔状态，支持重命名的旧路径和新路径。"""
    entries: list[StatusEntry] = []
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4:
            raise DiffError("invalid_git_status", "Git 返回了无法解析的状态")
        index_status, worktree_status = field[0], field[1]
        path = field[3:]
        status = _status_name(index_status, worktree_status)
        previous_path = None
        if status in {"renamed", "copied"}:
            if index >= len(fields) or not fields[index]:
                raise DiffError("invalid_git_status", "Git 重命名状态缺少目标路径")
            previous_path = fields[index]
            index += 1
        entries.append(StatusEntry(path, status, previous_path))
    return entries


def _safe_path(root: Path, relative: str) -> Path:
    """把 Git 返回的相对路径解析到仓库内，防止读取越界路径。"""
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise DiffError("invalid_git_path", f"Git 返回了仓库外路径：{relative}")
    return candidate


def _is_binary(data: bytes) -> bool:
    """用 NUL 字节快速判断未跟踪文件是否适合按文本生成 Diff。"""
    return b"\0" in data


def _count_patch(patch: str) -> tuple[int, int]:
    """统计统一 Diff 中的新增和删除行，不把文件头计入统计。"""
    additions = sum(line.startswith("+") and not line.startswith("+++")
                    for line in patch.splitlines())
    deletions = sum(line.startswith("-") and not line.startswith("---")
                    for line in patch.splitlines())
    return additions, deletions


def _untracked_patch(root: Path, relative: str) -> tuple[str, bool, bool, int]:
    """为未跟踪文本文件生成新增 Diff，并标记二进制或过大状态。"""
    path = _safe_path(root, relative)
    try:
        size = path.stat().st_size
        # 只读固定大小的前缀，避免检测大文件时把整个文件载入内存。
        with path.open("rb") as handle:
            sample = handle.read(8192)
    except OSError as exc:
        raise DiffError("file_read_failed", f"无法读取未跟踪文件 {relative}：{exc}") from exc
    if _is_binary(sample):
        return "", True, False, size
    if size > MAX_FILE_BYTES:
        return "", False, True, size
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "", True, False, size
    except OSError as exc:
        raise DiffError("file_read_failed", f"无法读取未跟踪文件 {relative}：{exc}") from exc
    patch = "".join(difflib.unified_diff(
        [], content.splitlines(keepends=True), fromfile="/dev/null", tofile=relative,
        n=80,
    ))
    return patch, False, False, size


def _git_blob_size(root: Path, relative: str) -> int | None:
    """读取 HEAD 中单个文件的字节数；不存在时返回空值。"""
    ok, output = worktrees.run_git_full(["cat-file", "-s", f"HEAD:{relative}"], root)
    if not ok:
        return None
    try:
        return int(output.strip())
    except ValueError:
        return None


def _tracked_numstat(root: Path, relative: str) -> tuple[int, int, bool]:
    """读取已跟踪文件的增删行数，并识别 Git 报告的二进制变更。"""
    raw = _run_git([
        "diff", "HEAD", "--no-ext-diff", "--no-color", "--numstat", "-z", "--", relative,
    ], root)
    field = raw.split("\0", 1)[0]
    if not field:
        return 0, 0, False
    parts = field.split("\t", 2)
    if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
        return 0, 0, True
    try:
        return int(parts[0]), int(parts[1]), False
    except ValueError as exc:
        raise DiffError("invalid_git_diff", "Git 返回了无法解析的行数统计") from exc


def _tracked_patch(root: Path, entry: StatusEntry) -> tuple[str, bool, bool, int | None, int, int]:
    """读取已跟踪文件的 Diff；大文件和二进制文件只返回元信息。"""
    additions, deletions, binary = _tracked_numstat(root, entry.path)
    path = _safe_path(root, entry.path)
    sizes: list[int] = []
    try:
        sizes.append(path.stat().st_size)
    except OSError:
        pass
    previous = entry.previous_path or entry.path
    previous_size = _git_blob_size(root, previous)
    if previous_size is not None:
        sizes.append(previous_size)
    size = max(sizes) if sizes else None
    if binary:
        return "", True, False, size, additions, deletions
    if size is not None and size > MAX_FILE_BYTES:
        return "", False, True, size, additions, deletions
    if path.exists():
        try:
            with path.open("rb") as handle:
                if _is_binary(handle.read(8192)):
                    return "", True, False, size, additions, deletions
        except OSError as exc:
            raise DiffError("file_read_failed", f"无法读取已跟踪文件 {entry.path}：{exc}") from exc
    patch = _run_git([
        "diff", "HEAD", "--no-ext-diff", "--no-color", "--binary", "--unified=80", "--", entry.path,
    ], root)
    if "Binary files" in patch or "GIT binary patch" in patch:
        return "", True, False, size, additions, deletions
    if not additions and not deletions:
        additions, deletions = _count_patch(patch)
    return patch, False, False, size, additions, deletions


def _limit_patch(patch: str, remaining: int) -> tuple[str, bool]:
    """限制单文件和整个响应的补丁字符数，保留截断事实。"""
    visible_limit = min(MAX_PATCH_CHARS, max(remaining, 0))
    visible = patch[:visible_limit]
    return visible, len(visible) != len(patch)


def collect_diff(workdir: Path) -> dict:
    """收集当前项目的 Git 状态和按文件拆分的只读 Diff 快照。"""
    root = _git_root(workdir)
    raw_status = _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    entries = sorted(_parse_status(raw_status), key=lambda item: item.path)
    files = []
    remaining = MAX_TOTAL_PATCH_CHARS
    for entry in entries:
        if entry.status == "untracked":
            patch, binary, file_truncated, size = _untracked_patch(root, entry.path)
            additions, deletions = _count_patch(patch)
        else:
            patch, binary, file_truncated, size, additions, deletions = _tracked_patch(root, entry)
        visible_patch, patch_truncated = _limit_patch(patch, remaining)
        remaining -= len(visible_patch)
        files.append({
            "path": entry.path,
            "previous_path": entry.previous_path,
            "status": entry.status,
            "additions": additions,
            "deletions": deletions,
            "binary": binary,
            "truncated": file_truncated or patch_truncated,
            "size_bytes": size,
            "patch": visible_patch,
        })
    return {
        "workdir": {
            "name": root.name,
            "branch": _branch(root),
            "commit": _commit(root),
        },
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
