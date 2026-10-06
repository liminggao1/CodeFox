"""任务板测试：归属、依赖图和存储边界。"""

from __future__ import annotations

import json

import pytest


def test_dependencies_gate_claim_and_completion_checks_owner(host_factory) -> None:
    host = host_factory()
    schema = host.create_task("create schema")
    api = host.create_task("write API")
    host.update_task(api.id, [schema.id])

    assert host.claim_task(api.id) == f"Blocked by: ['{schema.id}']"
    assert "Claimed" in host.claim_task(schema.id)
    assert "Unblocked: write API" in host.complete_task(schema.id)
    # 所有者的 cwd 租约在轮次边界才释放，因此执行 release_completed_assignment 前
    # 不允许再次领取任务。
    assert "must finish the current work turn" in host.claim_task(api.id)
    assert host.release_completed_assignment("agent") is True
    assert "Claimed" in host.claim_task(api.id)
    assert "owned by agent, not other" in host.complete_task(api.id, owner="other")
    assert "Completed" in host.complete_task(api.id)
    assert host.load_task(api.id).status == "completed"


def test_dependencies_are_added_after_create_returns_runtime_ids(host_factory) -> None:
    host = host_factory()
    created = [
        host.run_create_task(subject)
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
        assert not host.run_update_task(task_id, blockers).startswith("Error:")

    assert host.load_task(schema_id).blockedBy == []
    assert host.load_task(api_id).blockedBy == [schema_id]
    assert host.load_task(tests_id).blockedBy == [api_id]
    assert host.load_task(docs_id).blockedBy == [schema_id]


def test_invalid_and_missing_task_ids_become_tool_results(host_factory) -> None:
    host = host_factory()

    invalid = host.run_get_task("../outside")
    missing = host.run_claim_task("task_00000000")

    assert invalid.startswith("Error: Invalid task ID")
    assert missing.startswith("Error:")


def test_create_retries_instead_of_overwriting_an_existing_id(
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    values = iter(["deadbeef", "deadbeef", "cafebabe"])
    monkeypatch.setattr(host.tasks.secrets, "token_hex", lambda size: next(values))

    first = host.create_task("first")
    second = host.create_task("second")

    assert first.id == "task_deadbeef"
    assert second.id == "task_cafebabe"
    assert [task.subject for task in host.list_tasks()] == ["second", "first"]


def test_update_rejects_invalid_graph_changes_without_partial_mutation(
    host_factory,
) -> None:
    host = host_factory()
    dependency = host.create_task("create schema")
    target = host.create_task("write API")

    missing = host.run_update_task(
        target.id, [dependency.id, "task_00000000"]
    )
    self_dependency = host.run_update_task(target.id, [target.id])

    assert missing == "Error: Dependency not found: task_00000000"
    assert self_dependency == "Error: Task cannot depend on itself"
    assert host.load_task(target.id).blockedBy == []


def test_update_is_idempotent_and_rejects_cycles_or_started_tasks(
    host_factory,
) -> None:
    host = host_factory()
    first = host.create_task("first")
    second = host.create_task("second")
    third = host.create_task("third")

    host.update_task(second.id, [first.id, first.id])
    host.update_task(second.id, [first.id])
    host.update_task(third.id, [second.id])

    cycle = host.run_update_task(first.id, [third.id])
    assert cycle.startswith("Error: Dependency cycle detected")
    assert host.load_task(first.id).blockedBy == []
    assert host.load_task(second.id).blockedBy == [first.id]

    assert "Claimed" in host.claim_task(first.id)
    started = host.run_update_task(first.id, [second.id])
    assert "only be updated while pending and unowned" in started


def test_task_record_round_trips_through_json(host_factory) -> None:
    host = host_factory()
    task = host.create_task("Round trip", "details")

    payload = json.loads(host.get_task_json(task.id))

    assert payload == {
        "id": task.id,
        "subject": "Round trip",
        "description": "details",
        "status": "pending",
        "owner": None,
        "blockedBy": [],
        "worktree": None,
    }


def test_task_store_rejects_a_symlink_outside_the_workspace(
    host_factory, tmp_path
) -> None:
    host_factory()
    outside = tmp_path.parent / "outside-tasks"
    outside.mkdir()
    (tmp_path / ".tasks").symlink_to(outside, target_is_directory=True)

    # 符号链接 .tasks 指向工作区外，路径检查应拒绝写入，避免越过启动目录。
    from agent_harness import tasks

    with pytest.raises(ValueError, match="Invalid task ID"):
        tasks.create_task("unsafe")

    assert [path.name for path in outside.iterdir()] == [".lock"]
