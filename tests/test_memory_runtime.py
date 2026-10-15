"""Memory module boundary tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from codefox.context import memory_runtime
from codefox.providers.llm_client import estimate_input_tokens


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


def test_memory_recall_respects_serialized_token_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_runtime.core, "WORKDIR", tmp_path)
    monkeypatch.setattr(memory_runtime.core, "MEMORY_ENABLED", True)
    monkeypatch.setattr(memory_runtime.core, "MEMORY_RECALL_TOKEN_BUDGET", 120)
    path = memory_runtime.write_memory_file(
        "Long note", "project", "Long content", "中" * 1000
    )
    monkeypatch.setattr(
        memory_runtime, "select_relevant_memories", lambda messages: [path.name]
    )

    result = memory_runtime.load_memories([])
    recalled = json.loads(result)

    assert len(recalled[0]["content"]) < 1000
    assert estimate_input_tokens(
        messages=[{"role": "user", "content": result}]
    ) <= memory_runtime.core.MEMORY_RECALL_TOKEN_BUDGET


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
