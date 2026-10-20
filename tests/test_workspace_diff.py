"""工作区 Diff 服务的离线测试。"""

from __future__ import annotations

import subprocess

import pytest

from codefox.workspace import worktrees
from codefox.workspace.diff import MAX_FILE_BYTES, DiffError, collect_diff


def git(root, *args: str) -> str:
    """在临时仓库中执行 Git，返回标准输出。"""
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout


def init_repo(root):
    """创建一个带初始提交的临时仓库，避免测试依赖项目真实状态。"""
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Diff Test")
    (root / "tracked.txt").write_text("before\nkeep\nend\n", encoding="utf-8")
    (root / "removed.txt").write_text("remove me\n", encoding="utf-8")
    git(root, "add", "tracked.txt", "removed.txt")
    git(root, "commit", "-qm", "initial")


def files_by_path(snapshot):
    """把快照文件列表转换成便于断言的映射。"""
    return {item["path"]: item for item in snapshot["files"]}


def test_collect_diff_returns_clean_snapshot(tmp_path):
    init_repo(tmp_path)

    snapshot = collect_diff(tmp_path)

    assert snapshot["workdir"]["name"] == tmp_path.name
    assert snapshot["workdir"]["commit"]
    assert snapshot["files"] == []


def test_collect_diff_reports_tracked_and_untracked_changes(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("before\nchanged\nend\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new file\n", encoding="utf-8")
    (tmp_path / "staged.txt").write_text("staged file\n", encoding="utf-8")
    git(tmp_path, "add", "staged.txt")
    (tmp_path / "removed.txt").unlink()

    snapshot = collect_diff(tmp_path)
    files = files_by_path(snapshot)

    assert set(files) == {"new.txt", "removed.txt", "staged.txt", "tracked.txt"}
    assert files["tracked.txt"]["status"] == "modified"
    assert files["tracked.txt"]["additions"] == 1
    assert files["tracked.txt"]["deletions"] == 1
    assert "+changed" in files["tracked.txt"]["patch"]
    assert files["new.txt"]["status"] == "untracked"
    assert "+new file" in files["new.txt"]["patch"]
    assert files["staged.txt"]["status"] == "added"
    assert "+staged file" in files["staged.txt"]["patch"]
    assert files["removed.txt"]["status"] == "deleted"
    assert files["removed.txt"]["deletions"] == 1


def test_collect_diff_reports_renames_and_staged_changes(tmp_path):
    init_repo(tmp_path)
    git(tmp_path, "mv", "tracked.txt", "renamed.txt")
    (tmp_path / "renamed.txt").write_text("before\nchanged\nend\n", encoding="utf-8")
    git(tmp_path, "add", "renamed.txt")

    snapshot = collect_diff(tmp_path)
    files = files_by_path(snapshot)

    assert files["renamed.txt"]["status"] == "renamed"
    assert files["renamed.txt"]["previous_path"] == "tracked.txt"
    assert "+changed" in files["renamed.txt"]["patch"]


def test_collect_diff_marks_binary_and_large_untracked_files(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "binary.bin").write_bytes(b"header\0binary")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))

    files = files_by_path(collect_diff(tmp_path))

    assert files["binary.bin"]["binary"] is True
    assert files["binary.bin"]["patch"] == ""
    assert files["large.txt"]["binary"] is False
    assert files["large.txt"]["truncated"] is True
    assert files["large.txt"]["patch"] == ""


def test_collect_diff_limits_tracked_binary_and_large_files(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "binary.bin").write_bytes(b"header\0binary")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    git(tmp_path, "add", "binary.bin", "large.txt")
    git(tmp_path, "commit", "-qm", "large files")
    (tmp_path / "binary.bin").write_bytes(b"header\0changed")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 2))

    files = files_by_path(collect_diff(tmp_path))

    assert files["binary.bin"]["binary"] is True
    assert files["binary.bin"]["patch"] == ""
    assert files["large.txt"]["truncated"] is True
    assert files["large.txt"]["patch"] == ""


def test_collect_diff_reports_git_command_failure_without_traceback(tmp_path, monkeypatch):
    def failing_git(args, cwd=None):
        if args == ["rev-parse", "--show-toplevel"]:
            return True, str(tmp_path) + "\n"
        return False, "git command timed out"

    monkeypatch.setattr(worktrees, "run_git_full", failing_git)

    with pytest.raises(DiffError) as error:
        collect_diff(tmp_path)

    assert error.value.code == "git_command_failed"
    assert "Traceback" not in error.value.message


def test_collect_diff_rejects_non_git_directory(tmp_path):
    with pytest.raises(DiffError) as error:
        collect_diff(tmp_path)

    assert error.value.code == "not_git_repository"
