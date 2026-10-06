"""Flat 构建测试：生成的单文件与源码同步且可运行。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLAT_FILE = PROJECT_ROOT / "flat" / "agent_full.py"


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_flat", PROJECT_ROOT / "scripts" / "build_flat.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_is_deterministic() -> None:
    builder = load_builder()
    assert builder.build() == builder.build()


def test_committed_flat_file_is_current() -> None:
    builder = load_builder()
    assert FLAT_FILE.read_text(encoding="utf-8") == builder.build(), (
        "flat/agent_full.py is stale; run `python scripts/build_flat.py`"
    )


def test_only_the_final_section_has_a_main_guard() -> None:
    text = FLAT_FILE.read_text(encoding="utf-8")
    assert text.count('if __name__ == "__main__":') == 1
    assert text.rstrip().endswith("main(sys.argv[1:])")


def test_flat_file_compiles() -> None:
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(FLAT_FILE)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_flat_file_runs_the_workflow_demo(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(FLAT_FILE), "workflow", "demo"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert "status=completed" in result.stdout
    assert (tmp_path / ".runtime" / "last_run.txt").exists()
