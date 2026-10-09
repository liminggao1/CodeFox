"""编码测试：shell 输出解码与工作区文本读写。"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bash_decodes_arbitrary_bytes_without_crashing(host_factory) -> None:
    host = host_factory()

    output = host.run_bash(
        "printf 'ok\\xff\\xfebad'", cwd=host.core.WORKDIR
    )

    assert "ok" in output


def test_bash_merges_stderr_and_reports_empty_output(host_factory) -> None:
    host = host_factory()

    assert "boom" in host.run_bash(
        "echo boom 1>&2", cwd=host.core.WORKDIR
    )
    assert host.run_bash("true", cwd=host.core.WORKDIR) == "(no output)"


def test_bash_reports_a_nonzero_exit_code(host_factory) -> None:
    host = host_factory()

    assert "status 3" in host.run_bash("exit 3", cwd=host.core.WORKDIR)


def test_file_tools_round_trip_utf8_text(host_factory) -> None:
    host = host_factory()
    payload = "中文内容 ✓ emoji 🚀"

    assert host.run_write("notes/中文.txt", payload, cwd=host.core.WORKDIR) != ""
    assert host.run_read("notes/中文.txt", cwd=host.core.WORKDIR) == payload


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
