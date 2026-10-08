"""Lead worktree cleanup preserves leases until Git removal succeeds."""

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
        task = host.tasks.create_task("Cleanup task")
        assert host.create_worktree("feature", task.id).startswith("Worktree")
        assert host.claim_task(task.id, owner=owner).startswith("Claimed")
        return host, host.load_task(task.id), tmp_path / ".worktrees" / "feature", git

    return create


def assert_lease_preserved(host, task, checkout, version):
    assert checkout.is_dir()
    assert host.assignment_cwd(task.owner) == checkout
    assert host.tasks.assignment_versions[task.owner] == version
    assert host.load_task(task.id).worktree == "feature"


def test_lead_completes_and_removes_in_one_model_round(worktree_runtime, monkeypatch):
    host, task, checkout, git = worktree_runtime()
    (checkout / "tracked.txt").write_text("committed result\n", encoding="utf-8")
    git("add", "tracked.txt", cwd=checkout)
    git("commit", "-qm", "task result", cwd=checkout)
    version = host.tasks.assignment_versions["agent"]
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

    monkeypatch.setattr(host.core, "client", SimpleNamespace(
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
    assert "agent" not in host.assignments
    assert host.tasks.assignment_versions["agent"] == version + 1
    assert host.load_task(task.id).status == "completed"
    assert host.load_task(task.id).worktree is None
    assert host.assignment_cwd("agent") == host.core.WORKDIR
    assert (host.core.WORKDIR / "after-cleanup.txt").read_text() == "main directory"
    assert git("show", "wt/feature:tracked.txt").stdout == "committed result\n"
    assert git("show", "HEAD:tracked.txt").stdout == "initial\n"


def test_unfinished_task_cannot_be_removed(worktree_runtime):
    host, task, checkout, _ = worktree_runtime()
    version = host.tasks.assignment_versions["agent"]

    result = host.run_remove_worktree("feature")

    assert "complete it before removal" in result
    assert_lease_preserved(host, task, checkout, version)


@pytest.mark.parametrize("file", ["tracked.txt", "untracked.txt", "ignored.txt"])
def test_dirty_worktree_keeps_lead_assignment(worktree_runtime, file):
    host, task, checkout, _ = worktree_runtime()
    (checkout / file).write_text("unsaved result", encoding="utf-8")
    assert host.complete_task(task.id).startswith("Completed")
    version = host.tasks.assignment_versions["agent"]

    result = host.run_remove_worktree("feature")

    assert "uncommitted" in result
    assert (checkout / file).read_text() == "unsaved result"
    assert_lease_preserved(host, task, checkout, version)


def test_lead_cannot_release_subagent_lease(worktree_runtime):
    host, task, checkout, _ = worktree_runtime(owner="alice")
    assert host.complete_task(task.id, owner="alice").startswith("Completed")
    version = host.tasks.assignment_versions["alice"]

    result = host.run_remove_worktree("feature")

    assert "still in use by alice" in result
    assert_lease_preserved(host, task, checkout, version)


def test_running_background_command_keeps_lead_assignment(worktree_runtime):
    host, task, checkout, _ = worktree_runtime()
    assert host.complete_task(task.id).startswith("Completed")
    version = host.tasks.assignment_versions["agent"]
    host.scheduler.background_tasks["bg_test"] = {
        "status": "running", "cwd": str(checkout)}

    result = host.run_remove_worktree("feature")

    assert "running background command" in result
    assert_lease_preserved(host, task, checkout, version)


@pytest.mark.parametrize("operation", ["status", "remove"])
def test_git_failure_keeps_lead_assignment(worktree_runtime, monkeypatch, operation):
    host, task, checkout, _ = worktree_runtime()
    assert host.complete_task(task.id).startswith("Completed")
    version = host.tasks.assignment_versions["agent"]
    run_git = host.worktrees.run_git

    def fail(args, cwd=None):
        if (operation == "status" and args[0] == "status"
                or operation == "remove" and args[:2] == ["worktree", "remove"]):
            return False, "injected Git failure"
        return run_git(args, cwd)

    monkeypatch.setattr(host.worktrees, "run_git", fail)
    result = host.run_remove_worktree("feature")

    assert "injected Git failure" in result
    assert_lease_preserved(host, task, checkout, version)


def test_removed_directory_releases_lead_even_if_unbinding_save_fails(
        worktree_runtime, monkeypatch):
    host, task, checkout, git = worktree_runtime()
    assert host.complete_task(task.id).startswith("Completed")
    version = host.tasks.assignment_versions["agent"]

    def fail_save(task):
        raise OSError("injected save failure")

    monkeypatch.setattr(host.tasks, "save_task", fail_save)
    result = host.run_remove_worktree("feature")

    assert result.startswith("Partial success")
    assert "injected save failure" in result
    assert not checkout.exists()
    assert "agent" not in host.assignments
    assert host.tasks.assignment_versions["agent"] == version + 1
    assert host.assignment_cwd("agent") == host.core.WORKDIR
    assert host.load_task(task.id).worktree == "feature"
    assert git("show-ref", "--verify", "refs/heads/wt/feature").returncode == 0


def test_cleanup_preserves_leads_unrelated_assignment(worktree_runtime):
    host, task, checkout, _ = worktree_runtime(owner="alice")
    assert host.complete_task(task.id, owner="alice").startswith("Completed")
    assert host.release_completed_assignment("alice")
    other = host.tasks.create_task("Other Lead task")
    assert host.claim_task(other.id).startswith("Claimed")
    assignment = dict(host.assignments["agent"])
    version = host.tasks.assignment_versions["agent"]

    assert host.run_remove_worktree("feature").startswith("Worktree")

    assert not checkout.exists()
    assert host.assignments["agent"] == assignment
    assert host.tasks.assignment_versions["agent"] == version
    assert host.load_task(other.id).status == "in_progress"


def test_model_tool_cannot_force_discard_changes(worktree_runtime):
    host, task, checkout, _ = worktree_runtime()
    assert host.complete_task(task.id).startswith("Completed")
    schema = next(tool["input_schema"] for tool in host.BUILTIN_TOOLS
                  if tool["name"] == "remove_worktree")
    assert set(schema["properties"]) == {"name"}
    assert schema["additionalProperties"] is False
    result = host.file_tools.call_tool_handler(
        host.run_remove_worktree, {"name": "feature", "discard_changes": True},
        "remove_worktree")

    assert result.startswith("Error:")
    assert checkout.is_dir()
    assert "agent" in host.assignments
