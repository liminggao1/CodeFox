"""Background task and cron scheduler tests."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def bash_block(command: str, tool_id: str = "tool-1", background: bool = True):
    return SimpleNamespace(
        id=tool_id, name="bash", type="tool_use",
        input={"command": command, "run_in_background": background},
    )


def test_background_requires_an_explicit_bash_flag(host_factory) -> None:
    host = host_factory()

    assert not host.should_run_background("bash", {"command": "npm install"})
    assert host.should_run_background(
        "bash", {"command": "printf ready", "run_in_background": True}
    )
    assert not host.should_run_background(
        "write_file", {"run_in_background": True}
    )


def test_background_permission_is_checked_before_dispatch(
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    monkeypatch.setattr(host.CONSOLE, "ask", lambda prompt=None: "no")
    block = bash_block("rm -rf /tmp/example", background=True)
    history = [{"role": "user", "content": "Delete the directory"}]
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[block]),
        SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="text", text="Denied.")]),
    ]
    host.set_client(SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_: responses.pop(0))))

    host.agent_loop(history, host.update_context({}, history), "delete")

    assert not host.scheduler.background_tasks
    result = history[2]["content"][0]
    assert result["type"] == "tool_result"
    assert "Permission denied" in result["content"]


def test_completed_result_is_collected_once(host_factory) -> None:
    host = host_factory()
    task_id = host.start_background_task(bash_block("printf ready"), {})
    assert wait_until(
        lambda: host.scheduler.background_tasks
        .get(task_id, {}).get("status") == "completed"
    )

    notifications = host.collect_background_results()

    assert len(notifications) == 1
    assert f"<task_id>{task_id}</task_id>" in notifications[0]
    assert "<status>completed</status>" in notifications[0]
    assert "ready" in notifications[0]
    assert host.collect_background_results() == []
    assert not host.has_pending_background()


def test_import_does_not_start_cron_threads(host_factory) -> None:
    host_factory()

    assert not any(
        thread.name in {"cron-scheduler", "cron-queue-processor"}
        for thread in threading.enumerate()
    )


def test_cron_validation_and_matching(host_factory) -> None:
    host = host_factory()
    monday_at_nine = datetime(2026, 8, 10, 9, 0)

    assert host.scheduler.validate_cron("0 9 * * 1-5") is None
    assert host.scheduler.cron_matches("0 9 * * 1-5", monday_at_nine)
    assert not host.scheduler.cron_matches("30 9 * * 1-5", monday_at_nine)
    assert "hour" in host.scheduler.validate_cron("0 24 * * *")
    assert "Expected 5 fields" in host.scheduler.validate_cron("0 9 * *")


def test_cron_tools_schedule_list_and_cancel(host_factory) -> None:
    host = host_factory()

    scheduled = host.run_schedule_cron("0 9 * * 1-5", "standup")

    assert scheduled.startswith("Scheduled cron_")
    job_id = scheduled.split()[1].rstrip(":")
    assert "standup" in host.run_list_crons()
    assert host.run_cancel_cron(job_id) == f"Cancelled {job_id}"
    assert host.run_list_crons() == "No cron jobs."
    assert host.run_cancel_cron(job_id) == f"Job {job_id} not found"


def test_invalid_cron_expression_becomes_a_tool_error(host_factory) -> None:
    host = host_factory()

    result = host.run_schedule_cron("0 24 * * *", "bad")

    assert result.startswith("Error: hour")


def test_durable_jobs_round_trip_through_the_store(host_factory) -> None:
    host = host_factory()
    scheduled = host.run_schedule_cron("0 9 * * *", "persist me", durable=True)
    job_id = scheduled.split()[1].rstrip(":")

    payload = json.loads(host.scheduler._durable_path().read_text(encoding="utf-8"))

    assert [item["id"] for item in payload] == [job_id]
    assert payload[0]["prompt"] == "persist me"


def test_corrupt_durable_store_is_ignored(host_factory) -> None:
    host = host_factory()
    host.scheduler._durable_path().write_text("{broken", encoding="utf-8")

    host.scheduler.load_durable_jobs()

    # An unreadable store must not resurrect phantom jobs.
    assert host.scheduler.scheduled_jobs == {}


def test_restore_cron_jobs_does_not_duplicate_queue_entries(host_factory) -> None:
    host = host_factory()
    job = host.CronJob(id="cron_1", cron="0 9 * * *", prompt="p",
                       recurring=False, durable=False, pending_delivery=True)
    host.scheduler.scheduled_jobs[job.id] = job

    host.scheduler.restore_cron_jobs([job])
    host.scheduler.restore_cron_jobs([job])

    assert [queued.id for queued in host.scheduler.cron_queue] == ["cron_1"]
