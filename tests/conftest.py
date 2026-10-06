"""Shared test setup: project import path, safe env defaults, host factory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Keep imports offline and deterministic even without a local .env.
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("LLM_API", "responses")


def _reload_host() -> None:
    """Only agent.host is reloaded; goal and workflow keep module identity."""
    sys.modules.pop("agent_harness.host", None)


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    """Reload agent.host inside tmp_path so all state dirs follow the test."""

    def load():
        monkeypatch.chdir(tmp_path)
        _reload_host()
        import agent_harness.host as host

        # Paths and runtime state are module-global; a fresh session must not
        # inherit the previous test's launch directory, teammates, plan gates,
        # assignments, or MCP clients.
        host.core.WORKDIR = tmp_path
        host.teams.active_teammates.clear()
        host.teams.plan_gates.clear()
        host.teams.plan_gate_versions.clear()
        host.teams.plan_request_ids.clear()
        host.teams.pending_requests.clear()
        host.tasks.teammate_assignments.clear()
        host.tasks.assignment_versions.clear()
        host.tasks.CURRENT_TODOS.clear()
        host.mcp_clients.clear()
        host.scheduler.scheduled_jobs.clear()
        host.scheduler.cron_queue.clear()
        host.scheduler.background_tasks.clear()
        host.scheduler.background_results.clear()
        # Memory extraction would call the model; tests drive it explicitly.
        monkeypatch.setattr(host, "remember_after_turn", lambda messages: None)
        return host

    return load


@pytest.fixture
def workflow_module():
    from agent_harness import workflow

    return workflow
