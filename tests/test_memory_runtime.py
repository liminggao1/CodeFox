"""Memory module boundary tests."""

from __future__ import annotations

from pathlib import Path
import ast
import json
from types import SimpleNamespace

from agent_harness import memory_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_memory_runtime_exposes_memory_operations_only() -> None:
    """Memory retains its storage, retrieval, extraction and consolidation API."""
    for name in (
        "run_bash",
        "run_read",
        "run_write",
        "run_edit",
        "run_glob",
        "TOOLS",
        "TOOL_HANDLERS",
        "execute_tool",
        "agent_loop",
    ):
        assert not hasattr(memory_runtime, name)

    for name in (
        "read_memory_index",
        "read_memory_file",
        "list_memory_files",
        "load_memories",
        "extract_memories",
        "consolidate_memories",
    ):
        assert hasattr(memory_runtime, name)


def test_flat_memory_section_has_no_legacy_agent_runtime() -> None:
    """The generated file contains one Lead loop plus independent worker loops."""
    source = (PROJECT_ROOT / "src/agent_harness/memory_runtime.py").read_text(
        encoding="utf-8"
    )
    flat = (PROJECT_ROOT / "flat/agent_full.py").read_text(encoding="utf-8")

    legacy_markers = (
        "def run_bash(command: str)",
        "def run_read(path: str",
        "def execute_tool(block)",
        "# -- Agent 循环 --",
        "if __name__ == \"__main__\":",
    )
    for marker in legacy_markers:
        assert marker not in source

    memory_start = flat.index("# ==== src/agent_harness/memory_runtime.py ====")
    host_start = flat.index("# ==== src/agent_harness/host.py ====")
    memory_section = flat[memory_start:host_start]
    for marker in legacy_markers:
        assert marker not in memory_section

    assert "MEMORY_RUNTIME = sys.modules[__name__]" not in flat
    assert sum(
        isinstance(node, ast.FunctionDef) and node.name == "agent_loop"
        for node in ast.parse(flat).body
    ) == 1


def test_memory_recall_survives_model_failure(tmp_path, monkeypatch) -> None:
    """Recall preserves paths, source metadata and keyword fallback offline."""
    monkeypatch.setattr(memory_runtime.core, "WORKDIR", tmp_path)
    path = memory_runtime.write_memory_file(
        "Project layout", "project", "Source layout", "Use the src directory."
    )
    memory_runtime.rebuild_memory_index()

    def fail_model(**kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(
        memory_runtime.core, "client",
        SimpleNamespace(messages=SimpleNamespace(create=fail_model)),
    )
    recalled = json.loads(memory_runtime.load_memories([
        {"role": "user", "content": "Explain the project layout"}
    ]))
    assert path.parent == tmp_path / ".memory"
    assert path.name in memory_runtime.read_memory_index()
    assert recalled == [{"source": path.name, "content": path.read_text(encoding="utf-8")}]
    assert memory_runtime.extract_memories([
        {"role": "user", "content": "Remember the project layout"}
    ]) == 0
    assert memory_runtime.read_memory_file(path.name) == recalled[0]["content"]
