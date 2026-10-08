"""Tool snapshots preserve source ordering, closures and policy ownership."""

import pytest

from agent_harness import tool_registry


def test_mcp_snapshot_binds_each_handler_and_policy(host_factory):
    host = host_factory()
    before = tool_registry.REGISTRY.snapshot()
    host.connect_mcp("docs")
    host.connect_mcp("deploy")
    after = tool_registry.REGISTRY.snapshot()

    assert [tool["name"] for tool in before.tools] == [
        entry.schema["name"] for entry in host.registrations.BUILTIN_DEFINITIONS
    ] + ["Workflow"]
    assert "mcp__docs__search" not in before.handlers
    assert after.handlers["mcp__docs__get_version"]() == "[docs] API v2.1.0"
    assert after.handlers["mcp__deploy__status"](service="web") == "[deploy] web: running (v1.4.2)"
    assert after.policies["mcp__docs__get_version"] == "allow"
    assert after.policies["mcp__deploy__trigger"] == "confirm"
    with pytest.raises(TypeError):
        after.policies["mcp__deploy__trigger"] = "allow"


def test_failed_assembly_does_not_publish_partial_policies():
    registry = tool_registry.ToolRegistry()
    entries = [tool_registry.ToolDefinition({"name": "mcp__x__read"}, lambda: "ok", "first")]
    registry.configure(lambda: entries)
    registry.snapshot()
    entries.append(tool_registry.ToolDefinition({"name": "mcp__x__read"}, None, "second", "confirm"))
    with pytest.raises(ValueError, match="collision"):
        registry.snapshot()
    assert registry.policy_for("mcp__x__read") == "allow"


@pytest.mark.parametrize("actor", ["lead", "subagent"])
def test_task_listing_runs_through_actual_tool_handler(host_factory, actor):
    host = host_factory()
    handler = (tool_registry.REGISTRY.snapshot().handlers["list_tasks"] if actor == "lead"
               else host.subagent.create_subagent_session("alice").handlers["list_tasks"])
    assert handler() == "No tasks."
    task = host.tasks.create_task("List this task")
    listed = handler()
    assert task.id in listed
    assert "List this task [pending]" in listed
