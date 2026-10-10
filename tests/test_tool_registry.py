"""Tool snapshots preserve source ordering, closures and policy ownership."""

from agent_harness.runtime import tool_execution

import agent_harness.tools.mcp_tools as mcp_tools
import agent_harness.registrations as registrations
import agent_harness.agents.subagent as subagent_runtime
import agent_harness.workspace.tasks as tasks
import agent_harness.core as core
import agent_harness.workspace.task_worktrees as task_worktrees
import agent_harness.tools.file_tools as file_tools


import subprocess

import pytest

from agent_harness.tools import tool_registry


def test_mcp_snapshot_binds_each_handler_and_policy(host_factory):
    host = host_factory()
    before = tool_registry.REGISTRY.snapshot()
    mcp_tools.connect_mcp("docs")
    mcp_tools.connect_mcp("deploy")
    after = tool_registry.REGISTRY.snapshot()

    assert [tool["name"] for tool in before.tools] == [
        entry.schema["name"] for entry in registrations.BUILTIN_DEFINITIONS
    ] + ["Workflow"]
    assert "mcp__docs__search" not in before.handlers
    assert after.handlers["mcp__docs__get_version"]().content == "[docs] API v2.1.0"
    assert after.handlers["mcp__deploy__status"](service="web").content == "[deploy] web: running (v1.4.2)"
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
               else subagent_runtime.create_subagent_session("alice").handlers["list_tasks"])
    assert handler().content == "No tasks."
    task = tasks.create_task("List this task")
    listed = handler().content
    assert task.id in listed
    assert "List this task [pending]" in listed


def test_tool_schema_changes_do_not_leak_to_other_callers(host_factory):
    host = host_factory()
    first, _ = host.assemble_tool_pool()
    read = next(tool for tool in first if tool["name"] == "read_file")
    read["input_schema"]["properties"]["path"]["type"] = "integer"
    read["input_schema"]["required"].clear()

    second, _ = host.assemble_tool_pool()
    child = subagent_runtime.create_subagent_session("alice")
    for tools in (second, child.tools):
        read = next(tool for tool in tools if tool["name"] == "read_file")
        assert read["input_schema"]["properties"]["path"] == {"type": "string"}
        assert read["input_schema"]["required"] == ["path"]


@pytest.mark.parametrize("owner", ["agent", "alice"])
def test_bound_tools_follow_claim_complete_and_assignment_switch(host_factory, owner):
    host = host_factory()
    root = core.WORKDIR
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "tracked.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "initial"], cwd=root, check=True)
    handlers = (host.assemble_tool_pool()[1] if owner == "agent"
                else subagent_runtime.create_subagent_session(owner).handlers)
    handlers["write_file"]("actor.txt", "startup").content

    for name in ("first", "second"):
        task = tasks.create_task(f"Work in {name}")
        assert task_worktrees.create_worktree(name, task.id).startswith("Worktree")
        assert handlers["claim_task"](task.id).content.startswith("Claimed")
        assert handlers["write_file"]("actor.txt", name).content.startswith("Wrote")
        assert handlers["read_file"]("actor.txt").content == name
        assert handlers["edit_file"]("actor.txt", name, "edited").content.startswith("Edited")
        assert handlers["read_file"]("actor.txt").content == "edited"
        assert "actor.txt" in handlers["glob"]("actor.*").content
        assert str(root / ".worktrees" / name) in handlers["bash"]("pwd").content
        assert handlers["complete_task"](task.id).content.startswith("Completed")
        assert tasks.release_completed_assignment(owner)
        assert handlers["read_file"]("actor.txt").content == ("edited" if owner == "agent" else "startup")

    tasks.assignments[owner] = {"task_id": "task_00000000", "worktree": "missing"}
    assert handlers["write_file"]("actor.txt", "wrong").content.startswith("Error: Invalid task assignment:")
    assert (root / "actor.txt").read_text(encoding="utf-8") == "startup"


def test_child_bash_rejects_background_parameter_at_the_handler_boundary(host_factory):
    host = host_factory()
    child = subagent_runtime.create_subagent_session("alice")
    bash = next(tool for tool in child.tools if tool["name"] == "bash")
    assert "run_in_background" not in bash["input_schema"]["properties"]
    result = tool_execution.call_tool_handler(child.handlers["bash"],
                                    {"command": "touch forbidden.txt", "run_in_background": True}, "bash").content
    assert result.startswith("Error: TypeError:")
    assert not (core.WORKDIR / "forbidden.txt").exists()


@pytest.mark.parametrize("owner, label", [("agent", "task"), ("alice", "Task")])
def test_shared_task_tools_preserve_missing_task_text(host_factory, owner, label):
    host = host_factory()
    handlers = (host.assemble_tool_pool()[1] if owner == "agent"
                else subagent_runtime.create_subagent_session(owner).handlers)
    for name in ("claim_task", "complete_task"):
        assert handlers[name]("task_00000000").content == f"Error: {label} task_00000000 not found"
