"""任务板测试：归属、依赖图和存储边界。"""

from __future__ import annotations

from agent_harness import registrations

import agent_harness.workspace.tasks as tasks
import agent_harness.tools.shared_tools as shared_tools


import json

import pytest


def test_dependencies_gate_claim_and_completion_checks_owner(host_factory) -> None:
    host = host_factory()
    schema = tasks.create_task("create schema")
    api = tasks.create_task("write API")
    tasks.update_task(api.id, [schema.id])

    with pytest.raises(ValueError, match="Blocked by"):
        tasks.claim_task(api.id)
    assert "Claimed" in tasks.claim_task(schema.id)
    assert "Unblocked: write API" in tasks.complete_task(schema.id)
    # 所有者的 cwd 租约在轮次边界才释放，因此执行 release_completed_assignment 前
    # 不允许再次领取任务。
    with pytest.raises(ValueError, match="must finish the current work turn"):
        tasks.claim_task(api.id)
    assert tasks.release_completed_assignment("agent") is True
    assert "Claimed" in tasks.claim_task(api.id)
    with pytest.raises(ValueError, match="owned by agent, not other"):
        tasks.complete_task(api.id, owner="other")
    assert "Completed" in tasks.complete_task(api.id)
    assert tasks.load_task(api.id).status == "completed"


def test_dependencies_are_added_after_create_returns_runtime_ids(host_factory) -> None:
    host = host_factory()
    created = [
        registrations.run_create_task(subject).content
        for subject in ("create schema", "write API", "write tests", "write docs")
    ]
    schema_id, api_id, tests_id, docs_id = [
        result.split()[1].rstrip(":") for result in created
    ]

    for task_id, blockers in (
        (api_id, [schema_id]),
        (tests_id, [api_id]),
        (docs_id, [schema_id]),
    ):
        assert not registrations.run_update_task(task_id, blockers).content.startswith("Error:")

    assert tasks.load_task(schema_id).blockedBy == []
    assert tasks.load_task(api_id).blockedBy == [schema_id]
    assert tasks.load_task(tests_id).blockedBy == [api_id]
    assert tasks.load_task(docs_id).blockedBy == [schema_id]


def test_invalid_and_missing_task_ids_become_tool_results(host_factory) -> None:
    host = host_factory()

    invalid = registrations.run_get_task("../outside").content
    missing = shared_tools.run_claim_task("task_00000000").content

    assert invalid.startswith("Error: Invalid task ID")
    assert missing.startswith("Error:")


def test_create_retries_instead_of_overwriting_an_existing_id(
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    values = iter(["deadbeef", "deadbeef", "cafebabe"])
    monkeypatch.setattr(tasks.secrets, "token_hex", lambda size: next(values))

    first = tasks.create_task("first")
    second = tasks.create_task("second")

    assert first.id == "task_deadbeef"
    assert second.id == "task_cafebabe"
    assert [task.subject for task in tasks.list_tasks()] == ["second", "first"]


def test_update_rejects_invalid_graph_changes_without_partial_mutation(
    host_factory,
) -> None:
    host = host_factory()
    dependency = tasks.create_task("create schema")
    target = tasks.create_task("write API")

    missing = registrations.run_update_task(
        target.id, [dependency.id, "task_00000000"]
    ).content
    self_dependency = registrations.run_update_task(target.id, [target.id]).content

    assert missing == "Error: Dependency not found: task_00000000"
    assert self_dependency == "Error: Task cannot depend on itself"
    assert tasks.load_task(target.id).blockedBy == []


def test_update_is_idempotent_and_rejects_cycles_or_started_tasks(
    host_factory,
) -> None:
    host = host_factory()
    first = tasks.create_task("first")
    second = tasks.create_task("second")
    third = tasks.create_task("third")

    tasks.update_task(second.id, [first.id, first.id])
    tasks.update_task(second.id, [first.id])
    tasks.update_task(third.id, [second.id])

    cycle = registrations.run_update_task(first.id, [third.id]).content
    assert cycle.startswith("Error: Dependency cycle detected")
    assert tasks.load_task(first.id).blockedBy == []
    assert tasks.load_task(second.id).blockedBy == [first.id]

    assert "Claimed" in tasks.claim_task(first.id)
    started = registrations.run_update_task(first.id, [second.id]).content
    assert "only be updated while pending and unowned" in started


def test_task_record_round_trips_through_json(host_factory) -> None:
    host = host_factory()
    task = tasks.create_task("Round trip", "details")

    payload = json.loads(tasks.get_task_json(task.id))

    assert payload == {
        "id": task.id,
        "subject": "Round trip",
        "description": "details",
        "status": "pending",
        "owner": None,
        "blockedBy": [],
        "worktree": None,
        "session_id": None,
    }


def test_task_store_rejects_a_symlink_outside_the_workspace(
    host_factory, tmp_path
) -> None:
    host_factory()
    outside = tmp_path.parent / "outside-tasks"
    outside.mkdir()
    (tmp_path / ".tasks").symlink_to(outside, target_is_directory=True)

    # 符号链接 .tasks 指向工作区外，路径检查应拒绝写入，避免越过启动目录。
    from agent_harness.workspace import tasks

    with pytest.raises(ValueError, match="Invalid task ID"):
        tasks.create_task("unsafe")

    assert [path.name for path in outside.iterdir()] == [".lock"]
