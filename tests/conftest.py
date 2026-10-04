"""Shared test setup: project import path, safe env defaults, host factory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Keep imports offline and deterministic even without a local .env.
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("LLM_API", "responses")


def _reload_host() -> None:
    """Only agent.host is reloaded; goal and workflow keep module identity."""
    sys.modules.pop("agent.host", None)


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    """Reload agent.host inside tmp_path so all state dirs follow the test."""

    def load():
        monkeypatch.chdir(tmp_path)
        _reload_host()
        import agent.host as host

        # Memory extraction would call the model; tests drive it explicitly.
        monkeypatch.setattr(host, "remember_after_turn", lambda messages: None)
        return host

    return load


@pytest.fixture
def workflow_module():
    from agent import workflow

    return workflow