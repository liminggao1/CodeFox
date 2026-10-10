"""Memory module boundary tests."""

from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

from agent_harness.context import memory_runtime


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


def test_memory_source_has_no_legacy_agent_runtime() -> None:
    """Memory storage does not embed file tools or a Lead loop."""
    source = (PROJECT_ROOT / "src/agent_harness/context/memory_runtime.py").read_text(
        encoding="utf-8"
    )

    legacy_markers = (
        "def run_bash(command: str)",
        "def run_read(path: str",
        "def execute_tool(block)",
        "# -- Agent 循环 --",
        "if __name__ == \"__main__\":",
    )
    for marker in legacy_markers:
        assert marker not in source

def test_memory_recall_survives_model_failure(tmp_path, monkeypatch) -> None:
    """Recall preserves paths, source metadata and keyword fallback offline."""
    monkeypatch.setattr(memory_runtime.core, "WORKDIR", tmp_path)
    monkeypatch.setattr(memory_runtime.core, "MEMORY_ENABLED", True)
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


def test_memory_disabled_skips_recall_extraction_and_consolidation(tmp_path, monkeypatch) -> None:
    """The MVP default does not read or make model calls for optional memory."""
    monkeypatch.setattr(memory_runtime.core, "WORKDIR", tmp_path)
    monkeypatch.setattr(memory_runtime.core, "MEMORY_ENABLED", False)
    memory_runtime.write_memory_file(
        "Project layout", "project", "Source layout", "Use the src directory."
    )
    calls = []

    def fail_model(**kwargs):
        calls.append(kwargs)
        raise AssertionError("disabled memory must not call the model")

    monkeypatch.setattr(
        memory_runtime.core,
        "client",
        SimpleNamespace(messages=SimpleNamespace(create=fail_model)),
    )

    assert memory_runtime.read_memory_index() == ""
    assert memory_runtime.load_memories([
        {"role": "user", "content": "Explain the project layout"}
    ]) == ""
    assert memory_runtime.extract_memories([
        {"role": "user", "content": "Remember the project layout"}
    ]) == 0
    assert memory_runtime.consolidate_memories() == 0
    assert calls == []
