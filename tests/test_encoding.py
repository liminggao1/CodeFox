"""编码测试：shell 输出解码与工作区文本读写。"""

from __future__ import annotations

import agent_harness.tools.file_tools as file_tools
import agent_harness.core as core


from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bash_decodes_arbitrary_bytes_without_crashing(host_factory) -> None:
    host = host_factory()

    output = file_tools.run_bash(
        "printf 'ok\\xff\\xfebad'", cwd=core.WORKDIR
    ).content

    assert "ok" in output


def test_bash_merges_stderr_and_reports_empty_output(host_factory) -> None:
    host = host_factory()

    assert "boom" in file_tools.run_bash(
        "echo boom 1>&2", cwd=core.WORKDIR
    ).content
    assert file_tools.run_bash("true", cwd=core.WORKDIR).content == "(no output)"


def test_bash_reports_a_nonzero_exit_code(host_factory) -> None:
    host = host_factory()

    assert "status 3" in file_tools.run_bash("exit 3", cwd=core.WORKDIR).content


def test_file_tools_round_trip_utf8_text(host_factory) -> None:
    host = host_factory()
    payload = "中文内容 ✓ emoji 🚀"

    assert file_tools.run_write("notes/中文.txt", payload, cwd=core.WORKDIR).content != ""
    assert file_tools.run_read("notes/中文.txt", cwd=core.WORKDIR).content == payload


def test_repository_text_files_are_utf8_with_lf() -> None:
    targets = [
        REPO_ROOT / "src" / "agent_harness" / name
        for name in ("host.py", "core.py", "workspace/tasks.py", "runtime/scheduler.py")
    ]
    targets.append(REPO_ROOT / "README.md")

    for path in targets:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), path
        assert b"\r\n" not in raw, path
        raw.decode("utf-8")


def test_agent_harness_sources_decode_as_utf8() -> None:
    package = REPO_ROOT / "src" / "agent_harness"

    for path in sorted(package.rglob("*.py")):
        path.read_text(encoding="utf-8")
