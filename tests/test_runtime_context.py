"""Runtime snapshots refresh on the next model call without mutating old values."""

import agent_harness.context.memory_runtime as memory_runtime
import agent_harness.tools.mcp_tools as mcp_tools
import agent_harness.agents.subagent as subagent_runtime
import agent_harness.core as core


from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent_harness import runtime_context


def test_snapshot_owns_its_values(host_factory, monkeypatch) -> None:
    host = host_factory()
    monkeypatch.setattr(memory_runtime, "load_memories", lambda messages: "")
    first = runtime_context.build_context([])
    mcp_tools.connect_mcp("docs")
    subagent_runtime.active_subagents["alice"] = subagent_runtime.SubagentRecord("alice")
    second = runtime_context.build_context([])

    assert first.connected_mcp == ()
    assert first.active_subagents == ()
    assert second.connected_mcp == ("docs",)
    assert second.active_subagents == ("alice",)
    with pytest.raises(FrozenInstanceError):
        first.memories = "changed"


def test_agent_refreshes_memory_mcp_and_subagents_after_tool_round(host_factory, monkeypatch):
    host = host_factory(enable_memory=True)
    monkeypatch.setattr(host, "remember_after_turn", lambda messages: None)
    calls = []
    monkeypatch.setattr(memory_runtime, "load_memories", lambda messages: "recalled")

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            memory_runtime.write_memory_file("Layout", "project", "src layout", "src/")
            memory_runtime.rebuild_memory_index()
            subagent_runtime.active_subagents["alice"] = "working"
            content = [SimpleNamespace(
                type="tool_use", id="connect", name="connect_mcp", input={"name": "docs"}
            )]
            return SimpleNamespace(content=content, stop_reason="tool_use")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Done")], stop_reason="end_turn"
        )

    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    host.agent_loop([{"role": "user", "content": "connect docs"}], "connect docs")

    assert len(calls) == 2
    assert "Connected MCP servers: docs" not in calls[0]["system"]
    assert "Connected MCP servers: docs" in calls[1]["system"]
    assert "Active subagents: alice" in calls[1]["system"]
    assert "layout.md" in calls[1]["system"]
    assert "Relevant memory records:\nrecalled" in calls[1]["system"]
    assert any(tool["name"] == "mcp__docs__search" for tool in calls[1]["tools"])
