"""后台任务与 cron 调度器测试。"""

from __future__ import annotations

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
    host.core.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_: responses.pop(0)))

    host.agent_loop(history, "delete")

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


def test_pending_one_shot_is_requeued_after_restart(host_factory) -> None:
    host = host_factory()
    job = host.schedule_job("0 9 * * *", "persist me", recurring=False)
    host.scheduler._enqueue_due_job(job)
    host.scheduler.scheduled_jobs.clear()
    host.scheduler.cron_queue.clear()

    host.scheduler.load_durable_jobs()

    assert [queued.id for queued in host.scheduler.cron_queue] == [job.id]
    restored = host.scheduler.scheduled_jobs[job.id]
    host.scheduler.load_durable_jobs()
    assert host.scheduler.cron_queue == [restored]
    assert host.scheduler.scheduled_jobs[job.id] is restored
    delivered = host.consume_cron_queue()
    # Reinitialization must not requeue a delivery already being processed.
    host.scheduler.load_durable_jobs()
    assert not host.scheduler.cron_queue
    host.acknowledge_cron_jobs(delivered)
    host.scheduler.scheduled_jobs.clear()
    host.scheduler.load_durable_jobs()
    assert host.scheduler.scheduled_jobs == {}


def test_only_runtime_startup_loads_pending_cron(host_factory, monkeypatch) -> None:
    host = host_factory()
    job = host.schedule_job("0 9 * * *", "persist me", recurring=False)
    host.scheduler._enqueue_due_job(job)
    host.scheduler.scheduled_jobs.clear()
    host.scheduler.cron_queue.clear()
    load = host.scheduler.load_durable_jobs
    calls = []
    def load_once():
        calls.append(True)
        load()
    monkeypatch.setattr(host.scheduler, "load_durable_jobs", load_once)
    importlib.reload(host)
    assert calls == [] and not host.scheduler.cron_queue
    try:
        host.start_runtime_services()
        host.start_runtime_services()
        assert calls == [True]
        assert [queued.id for queued in host.scheduler.cron_queue] == [job.id]
    finally:
        host.scheduler.stop_runtime_services()


def test_schedule_write_failure_does_not_register_job(host_factory, monkeypatch) -> None:
    host = host_factory()

    monkeypatch.setattr(host.scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        host.schedule_job("0 9 * * *", "persist me")

    assert host.scheduler.scheduled_jobs == {}
    assert not host.scheduler._durable_path().exists()


def test_cancel_write_failure_keeps_job_and_queue(host_factory, monkeypatch) -> None:
    host = host_factory()
    job = host.schedule_job("0 9 * * *", "persist me", recurring=False)
    host.scheduler._enqueue_due_job(job)
    disk_before = host.scheduler._durable_path().read_text(encoding="utf-8")

    monkeypatch.setattr(host.scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        host.cancel_job(job.id)

    assert host.scheduler.scheduled_jobs[job.id] is job
    assert host.scheduler.cron_queue == [job]
    assert host.scheduler._durable_path().read_text(encoding="utf-8") == disk_before


def test_enqueue_write_failure_keeps_job_due(host_factory, monkeypatch) -> None:
    host = host_factory()
    job = host.schedule_job("0 9 * * *", "persist me", recurring=False)

    monkeypatch.setattr(host.scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        host.scheduler._enqueue_due_job(job)

    assert job.pending_delivery is False
    assert host.scheduler.cron_queue == []
    assert json.loads(host.scheduler._durable_path().read_text())[0][
        "pending_delivery"] is False


def test_acknowledge_write_failure_retains_pending_delivery(
    host_factory, monkeypatch
) -> None:
    host = host_factory()
    job = host.schedule_job("0 9 * * *", "persist me", recurring=False)
    host.scheduler._enqueue_due_job(job)
    delivered = host.consume_cron_queue()

    monkeypatch.setattr(host.scheduler.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        host.acknowledge_cron_jobs(delivered)

    assert host.scheduler.scheduled_jobs[job.id] is job
    assert job.pending_delivery is True
    assert host.consume_cron_queue() == [job]
    assert json.loads(host.scheduler._durable_path().read_text())[0][
        "pending_delivery"] is True


def test_acknowledge_failure_keeps_the_model_response(
    host_factory, monkeypatch, capsys
) -> None:
    host = host_factory()
    job = host.schedule_job("0 9 * * *", "persist me", recurring=False)
    host.scheduler._enqueue_due_job(job)
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Done")],
    )
    host.core.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: response)
    )
    monkeypatch.setattr(host.scheduler.os, "replace", fail_replace)
    history = [{"role": "user", "content": "Continue"}]

    host.agent_loop(history, "Continue")

    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == response.content
    assert host.consume_cron_queue() == [job]
    assert "Delivery confirmation failed" in capsys.readouterr().out


def test_corrupt_durable_store_is_ignored(host_factory) -> None:
    host = host_factory()
    host.scheduler._durable_path().write_text("{broken", encoding="utf-8")

    host.scheduler.load_durable_jobs()

    # 存储文件不可读时，不能恢复出不存在的任务。
    assert host.scheduler.scheduled_jobs == {}


def test_restore_cron_jobs_does_not_duplicate_queue_entries(host_factory) -> None:
    host = host_factory()
    job = host.CronJob(id="cron_1", cron="0 9 * * *", prompt="p",
                       recurring=False, durable=False, pending_delivery=True)
    host.scheduler.scheduled_jobs[job.id] = job

    host.scheduler.restore_cron_jobs([job])
    host.scheduler.restore_cron_jobs([job])

    assert [queued.id for queued in host.scheduler.cron_queue] == ["cron_1"]
