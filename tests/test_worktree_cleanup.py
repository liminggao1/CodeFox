"""Lead worktree cleanup preserves leases until Git removal succeeds."""

from agent_harness.runtime import tool_execution

import agent_harness.workspace.tasks as tasks
import agent_harness.workspace.task_worktrees as task_worktrees
import agent_harness.core as core
import agent_harness.tools.shared_tools as shared_tools
import agent_harness.runtime.scheduler as scheduler
import agent_harness.workspace.worktrees as worktrees
import agent_harness.registrations as registrations
import agent_harness.tools.file_tools as file_tools


import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def worktree_runtime(host_factory, tmp_path):
    host = host_factory()

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=cwd, check=True, capture_output=True, text=True)

    git("init", "-q")
    (tmp_path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git("add", "tracked.txt", ".gitignore")
    git("commit", "-qm", "initial")

    def create(owner="agent"):
        task = tasks.create_task("Cleanup task")
        assert task_worktrees.create_worktree("feature", task.id).startswith("Worktree")
        assert tasks.claim_task(task.id, owner=owner).startswith("Claimed")
        return host, tasks.load_task(task.id), tmp_path / ".worktrees" / "feature", git

    return create


def assert_lease_preserved(host, task, checkout, version):
    assert checkout.is_dir()
    assert tasks.assignment_cwd(task.owner) == checkout
    assert tasks.assignment_versions[task.owner] == version
    assert tasks.load_task(task.id).worktree == "feature"


def test_lead_completes_and_removes_in_one_model_round(worktree_runtime, monkeypatch):
    host, task, checkout, git = worktree_runtime()
    (checkout / "tracked.txt").write_text("committed result\n", encoding="utf-8")
    git("add", "tracked.txt", cwd=checkout)
    git("commit", "-qm", "task result", cwd=checkout)
    version = tasks.assignment_versions["agent"]
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", id="complete", name="complete_task",
                            input={"task_id": task.id}),
            SimpleNamespace(type="tool_use", id="remove", name="remove_worktree",
                            input={"name": "feature"}),
            SimpleNamespace(type="tool_use", id="write", name="write_file",
                            input={"path": "after-cleanup.txt", "content": "main directory"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="text", text="Cleanup finished")]),
    ]
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(core, "client", SimpleNamespace(
        messages=SimpleNamespace(create=respond)))
    history = [{"role": "user", "content": "Finish and clean up the task"}]
    host.agent_loop(history, "Finish and clean up the task")

    assert len(requests) == 2
    assert "remove_worktree" in {tool["name"] for tool in requests[0]["tools"]}
    results = next(message["content"] for message in history
                   if message["role"] == "user" and isinstance(message["content"], list))
    assert results[0]["content"].startswith("Completed")
    assert "Lead assignment released" in results[1]["content"]
    assert "default work directory" in results[1]["content"]
    assert not checkout.exists()
    assert "agent" not in tasks.assignments
    assert tasks.assignment_versions["agent"] == version + 1
    assert tasks.load_task(task.id).status == "completed"
    assert tasks.load_task(task.id).worktree is None
    assert tasks.assignment_cwd("agent") == core.WORKDIR
    assert (core.WORKDIR / "after-cleanup.txt").read_text() == "main directory"
    assert git("show", "wt/feature:tracked.txt").stdout == "committed result\n"
    assert git("show", "HEAD:tracked.txt").stdout == "initial\n"


def test_unfinished_task_cannot_be_removed(worktree_runtime):
    host, task, checkout, _ = worktree_runtime()
    version = tasks.assignment_versions["agent"]

    result = registrations.run_remove_worktree("feature").content

    assert "complete it before removal" in result
    assert_lease_preserved(host, task, checkout, version)


@pytest.mark.parametrize("file", ["tracked.txt", "untracked.txt", "ignored.txt"])
def test_dirty_worktree_keeps_lead_assignment(worktree_runtime, file):
    host, task, checkout, _ = worktree_runtime()
    (checkout / file).write_text("unsaved result", encoding="utf-8")
    assert tasks.complete_task(task.id).startswith("Completed")
    version = tasks.assignment_versions["agent"]

    result = registrations.run_remove_worktree("feature").content

    assert "uncommitted" in result
    assert (checkout / file).read_text() == "unsaved result"
    assert_lease_preserved(host, task, checkout, version)


def test_lead_cannot_release_subagent_lease(worktree_runtime):
    host, task, checkout, _ = worktree_runtime(owner="alice")
    assert tasks.complete_task(task.id, owner="alice").startswith("Completed")
    version = tasks.assignment_versions["alice"]

    result = registrations.run_remove_worktree("feature").content

    assert "still in use by alice" in result
    assert_lease_preserved(host, task, checkout, version)


def test_running_background_command_keeps_lead_assignment(worktree_runtime):
    host, task, checkout, _ = worktree_runtime()
    assert tasks.complete_task(task.id).startswith("Completed")
    version = tasks.assignment_versions["agent"]
    scheduler.background_tasks["bg_test"] = {
        "status": "running", "cwd": str(checkout)}

    result = registrations.run_remove_worktree("feature").content

    assert "running background command" in result
    assert_lease_preserved(host, task, checkout, version)


@pytest.mark.parametrize("operation", ["status", "remove"])
def test_git_failure_keeps_lead_assignment(worktree_runtime, monkeypatch, operation):
    host, task, checkout, _ = worktree_runtime()
    assert tasks.complete_task(task.id).startswith("Completed")
    version = tasks.assignment_versions["agent"]
    run_git = worktrees.run_git

    def fail(args, cwd=None):
        if (operation == "status" and args[0] == "status"
                or operation == "remove" and args[:2] == ["worktree", "remove"]):
            return False, "injected Git failure"
        return run_git(args, cwd)

    monkeypatch.setattr(worktrees, "run_git", fail)
    result = registrations.run_remove_worktree("feature").content

    assert "injected Git failure" in result
    assert_lease_preserved(host, task, checkout, version)


def test_removed_directory_releases_lead_even_if_unbinding_save_fails(
        worktree_runtime, monkeypatch):
    host, task, checkout, git = worktree_runtime()
    assert tasks.complete_task(task.id).startswith("Completed")
    version = tasks.assignment_versions["agent"]

    def fail_save(task):
        raise OSError("injected save failure")

    monkeypatch.setattr(tasks, "save_task", fail_save)
    result = registrations.run_remove_worktree("feature").content

    assert result.startswith("Partial success")
    assert "injected save failure" in result
    assert not checkout.exists()
    assert "agent" not in tasks.assignments
    assert tasks.assignment_versions["agent"] == version + 1
    assert tasks.assignment_cwd("agent") == core.WORKDIR
    assert tasks.load_task(task.id).worktree == "feature"
    assert git("show-ref", "--verify", "refs/heads/wt/feature").returncode == 0


def test_cleanup_preserves_leads_unrelated_assignment(worktree_runtime):
    host, task, checkout, _ = worktree_runtime(owner="alice")
    assert tasks.complete_task(task.id, owner="alice").startswith("Completed")
    assert tasks.release_completed_assignment("alice")
    other = tasks.create_task("Other Lead task")
    assert tasks.claim_task(other.id).startswith("Claimed")
    assignment = dict(tasks.assignments["agent"])
    version = tasks.assignment_versions["agent"]

    assert registrations.run_remove_worktree("feature").content.startswith("Worktree")

    assert not checkout.exists()
    assert tasks.assignments["agent"] == assignment
    assert tasks.assignment_versions["agent"] == version
    assert tasks.load_task(other.id).status == "in_progress"


def test_model_tool_cannot_force_discard_changes(worktree_runtime):
    host, task, checkout, _ = worktree_runtime()
    assert tasks.complete_task(task.id).startswith("Completed")
    schema = next(tool["input_schema"] for tool in registrations.BUILTIN_TOOLS
                  if tool["name"] == "remove_worktree")
    assert set(schema["properties"]) == {"name"}
    assert schema["additionalProperties"] is False
    result = tool_execution.call_tool_handler(
        registrations.run_remove_worktree, {"name": "feature", "discard_changes": True},
        "remove_worktree").content

    assert result.startswith("Error:")
    assert checkout.is_dir()
    assert "agent" in tasks.assignments
