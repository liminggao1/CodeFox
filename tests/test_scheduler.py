"""后台任务与 cron 调度器测试。"""

from __future__ import annotations

import agent_harness.runtime.scheduler as scheduler
import agent_harness.core as core


import json
import importlib
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


def fail_replace(*_args):
    raise OSError("disk unavailable")


def bash_block(command: str, tool_id: str = "tool-1", background: bool = True):
    return SimpleNamespace(
        id=tool_id, name="bash", type="tool_use",
        input={"command": command, "run_in_background": background},
    )


def test_background_requires_an_explicit_bash_flag(host_factory) -> None:
    host = host_factory()

    assert not scheduler.should_run_background("bash", {"command": "npm install"})
    assert scheduler.should_run_background(
        "bash", {"command": "printf ready", "run_in_background": True}
    )
    assert not scheduler.should_run_background(
        "write_file", {"run_in_background": True}
    )


def test_background_permission_is_checked_before_dispatch(
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    monkeypatch.setattr(core.CONSOLE, "ask", lambda prompt=None: "no")
    block = bash_block("rm -rf /tmp/example", background=True)
    history = [{"role": "user", "content": "Delete the directory"}]
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[block]),
        SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="text", text="Denied.")]),
    ]
    core.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_: responses.pop(0)))

    host.agent_loop(history, "delete")

    assert not scheduler.background_tasks
    result = history[2]["content"][0]
    assert result["type"] == "tool_result"
    assert "Permission denied" in result["content"]


def test_completed_result_is_collected_once(host_factory) -> None:
    host = host_factory()
    task_id = scheduler.start_background_task(bash_block("printf ready"), {})
    assert wait_until(
        lambda: scheduler.background_tasks
        .get(task_id, {}).get("status") == "completed"
    )

    notifications = scheduler.collect_background_results()

    assert len(notifications) == 1
    assert f"<task_id>{task_id}</task_id>" in notifications[0]
    assert "<status>completed</status>" in notifications[0]
    assert "ready" in notifications[0]
    assert scheduler.collect_background_results() == []
    assert not scheduler.has_pending_background()


def test_import_does_not_start_cron_threads(host_factory) -> None:
    host_factory()

    assert not any(
        thread.name in {"cron-scheduler", "cron-queue-processor"}
        for thread in threading.enumerate()
    )


def test_cron_validation_and_matching(host_factory) -> None:
    host = host_factory()
    monday_at_nine = datetime(2026, 8, 10, 9, 0)

    assert scheduler.validate_cron("0 9 * * 1-5") is None
    assert scheduler.cron_matches("0 9 * * 1-5", monday_at_nine)
    assert not scheduler.cron_matches("30 9 * * 1-5", monday_at_nine)
    assert "hour" in scheduler.validate_cron("0 24 * * *")
    assert "Expected 5 fields" in scheduler.validate_cron("0 9 * *")


def test_cron_tools_schedule_list_and_cancel(host_factory) -> None:
    host = host_factory()

    scheduled = scheduler.run_schedule_cron("0 9 * * 1-5", "standup").content

    assert scheduled.startswith("Scheduled cron_")
    job_id = scheduled.split()[1].rstrip(":")
    assert "standup" in scheduler.run_list_crons().content
    assert scheduler.run_cancel_cron(job_id).content == f"Cancelled {job_id}"
    assert scheduler.run_list_crons().content == "No cron jobs."
    assert scheduler.run_cancel_cron(job_id).content == f"Job {job_id} not found"


def test_invalid_cron_expression_becomes_a_tool_error(host_factory) -> None:
    host = host_factory()

    result = scheduler.run_schedule_cron("0 24 * * *", "bad").content

    assert result.startswith("Error: hour")


def test_durable_jobs_round_trip_through_the_store(host_factory) -> None:
    host = host_factory()
    scheduled = scheduler.run_schedule_cron("0 9 * * *", "persist me", durable=True).content
    job_id = scheduled.split()[1].rstrip(":")

    payload = json.loads(scheduler._durable_path().read_text(encoding="utf-8"))

    assert [item["id"] for item in payload] == [job_id]
    assert payload[0]["prompt"] == "persist me"


def test_pending_one_shot_is_requeued_after_restart(host_factory) -> None:
    host = host_factory()
    job = scheduler.schedule_job("0 9 * * *", "persist me", recurring=False)
    scheduler._enqueue_due_job(job)
    scheduler.scheduled_jobs.clear()
    scheduler.cron_queue.clear()

    scheduler.load_durable_jobs()

    assert [queued.id for queued in scheduler.cron_queue] == [job.id]
    restored = scheduler.scheduled_jobs[job.id]
    scheduler.load_durable_jobs()
    assert scheduler.cron_queue == [restored]
    assert scheduler.scheduled_jobs[job.id] is restored
    delivered = scheduler.consume_cron_queue()
    # Reinitialization must not requeue a delivery already being processed.
    scheduler.load_durable_jobs()
    assert not scheduler.cron_queue
    scheduler.acknowledge_cron_jobs(delivered)
    scheduler.scheduled_jobs.clear()
    scheduler.load_durable_jobs()
    assert scheduler.scheduled_jobs == {}


def test_only_runtime_startup_loads_pending_cron(host_factory, monkeypatch) -> None:
    host = host_factory()
    job = scheduler.schedule_job("0 9 * * *", "persist me", recurring=False)
    scheduler._enqueue_due_job(job)
    scheduler.scheduled_jobs.clear()
    scheduler.cron_queue.clear()
    load = scheduler.load_durable_jobs
    calls = []
    def load_once():
        calls.append(True)
        load()
    monkeypatch.setattr(scheduler, "load_durable_jobs", load_once)
    importlib.reload(host)
    assert calls == [] and not scheduler.cron_queue
    try:
        scheduler.start_runtime_services()
        scheduler.start_runtime_services()
        assert calls == [True]
        assert [queued.id for queued in scheduler.cron_queue] == [job.id]
    finally:
        scheduler.stop_runtime_services()


def test_schedule_write_failure_does_not_register_job(host_factory, monkeypatch) -> None:
    host = host_factory()

    monkeypatch.setattr(scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        scheduler.schedule_job("0 9 * * *", "persist me")

    assert scheduler.scheduled_jobs == {}
    assert not scheduler._durable_path().exists()


def test_cancel_write_failure_keeps_job_and_queue(host_factory, monkeypatch) -> None:
    host = host_factory()
    job = scheduler.schedule_job("0 9 * * *", "persist me", recurring=False)
    scheduler._enqueue_due_job(job)
    disk_before = scheduler._durable_path().read_text(encoding="utf-8")

    monkeypatch.setattr(scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        scheduler.cancel_job(job.id)

    assert scheduler.scheduled_jobs[job.id] is job
    assert scheduler.cron_queue == [job]
    assert scheduler._durable_path().read_text(encoding="utf-8") == disk_before


def test_enqueue_write_failure_keeps_job_due(host_factory, monkeypatch) -> None:
    host = host_factory()
    job = scheduler.schedule_job("0 9 * * *", "persist me", recurring=False)

    monkeypatch.setattr(scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        scheduler._enqueue_due_job(job)

    assert job.pending_delivery is False
    assert scheduler.cron_queue == []
    assert json.loads(scheduler._durable_path().read_text())[0][
        "pending_delivery"] is False


def test_acknowledge_write_failure_retains_pending_delivery(
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    job = scheduler.schedule_job("0 9 * * *", "persist me", recurring=False)
    scheduler._enqueue_due_job(job)
    delivered = scheduler.consume_cron_queue()

    monkeypatch.setattr(scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        scheduler.acknowledge_cron_jobs(delivered)

    assert scheduler.scheduled_jobs[job.id] is job
    assert job.pending_delivery is True
    assert scheduler.consume_cron_queue() == [job]
    assert json.loads(scheduler._durable_path().read_text())[0][
        "pending_delivery"] is True


def test_acknowledge_failure_keeps_the_model_response(
    host_factory, monkeypatch, capsys
) -> None:
    host = host_factory()
    job = scheduler.schedule_job("0 9 * * *", "persist me", recurring=False)
    scheduler._enqueue_due_job(job)
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Done")],
    )
    core.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: response)
    )
    monkeypatch.setattr(scheduler.os, "replace", fail_replace)
    history = [{"role": "user", "content": "Continue"}]

    host.agent_loop(history, "Continue")

    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == response.content
    assert scheduler.consume_cron_queue() == [job]
    assert "Delivery confirmation failed" in capsys.readouterr().out


def test_corrupt_durable_store_is_ignored(host_factory) -> None:
    host = host_factory()
    scheduler._durable_path().write_text("{broken", encoding="utf-8")

    scheduler.load_durable_jobs()

    # 存储文件不可读时，不能恢复出不存在的任务。
    assert scheduler.scheduled_jobs == {}


def test_restore_cron_jobs_does_not_duplicate_queue_entries(host_factory) -> None:
    host = host_factory()
    job = scheduler.CronJob(id="cron_1", cron="0 9 * * *", prompt="p",
                       recurring=False, durable=False, pending_delivery=True)
    scheduler.scheduled_jobs[job.id] = job

    scheduler.restore_cron_jobs([job])
    scheduler.restore_cron_jobs([job])

    assert [queued.id for queued in scheduler.cron_queue] == ["cron_1"]
