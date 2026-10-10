"""后台任务与 cron 调度器：调度、结果收集，以及完整后台输出只交付一次。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import importlib
import json
import threading
import time

import pytest

import agent_harness.context.compaction as compaction
from agent_harness.agents.lead_turn import LeadTurn
import agent_harness.core as core
import agent_harness.registrations as registrations
import agent_harness.runtime.hooks as hooks
import agent_harness.runtime.scheduler as scheduler
import agent_harness.tools.file_tools as file_tools
import agent_harness.workspace.tasks as tasks
import agent_harness.workspace.worktrees as worktrees


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


def finish_task(host, monkeypatch, output, exit_code=0):
    monkeypatch.setattr(scheduler.threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))
    monkeypatch.setattr(file_tools, "_run_bash_process", lambda command, cwd: (output, exit_code))
    block = SimpleNamespace(type="tool_use", id="tool", name="bash", input={"command": "fake"})
    return scheduler.start_background_task(block, {})


def full_path(note):
    return Path(note.split("<full_output>")[1].split("</full_output>")[0])


@pytest.mark.parametrize("length,exit_code,hook_failure", [(201, 0, False), (5000, 0, False),
                                                         (250, 7, False), (250, 0, True)])
def test_long_output_tail_is_readable(host_factory, monkeypatch, length, exit_code, hook_failure):
    host = host_factory()
    output = "中" * length + "TAIL"
    if hook_failure:
        def fail_hook(*args):
            raise RuntimeError("hook failed")
        monkeypatch.setattr(hooks, "trigger_hooks", fail_hook)
    task_id = finish_task(host, monkeypatch, output, exit_code)
    expected = scheduler.background_results[task_id]
    result = scheduler.background_tasks[task_id]["result"]
    assert result.status == ("failed" if exit_code else "success")
    note, = scheduler.collect_background_results()
    path = full_path(note)
    assert path.parent == core.tool_results_dir() / "background"
    assert path.read_text(encoding="utf-8") == expected
    assert "TAIL" in registrations.BUILTIN_HANDLERS["read_file"](str(path)).content
    assert expected[:200] in note
    assert f"<status>{'failed' if exit_code or hook_failure else 'completed'}</status>" in note
    assert f"<command_status>{result.status}</command_status>" in note
    assert ("hook failed" in note) is hook_failure
    assert scheduler.collect_background_results() == []


def test_bound_lead_reads_background_full_output_through_tool_loop(host_factory, monkeypatch):
    host = host_factory()
    checkout = core.WORKDIR / ".worktrees" / "feature"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(worktrees, "_registered_worktree", lambda name: (checkout, None))
    task = tasks.create_task("Read background evidence")
    task.worktree = "feature"
    tasks.save_task(task)
    assert tasks.claim_task(task.id, owner="agent").startswith("Claimed")
    task_id = finish_task(host, monkeypatch, "中" * 250 + "\nTAIL")
    assert scheduler.background_tasks[task_id]["cwd"] == str(checkout)
    note, = scheduler.collect_background_results()
    path = full_path(note)
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
            type="tool_use", id="read-evidence", name="read_file",
            input={"path": str(path), "offset": 1, "limit": 1})]),
        SimpleNamespace(stop_reason="end_turn", content=[]),
    ]
    monkeypatch.setattr(core, "client", SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: responses.pop(0))))
    history = [{"role": "user", "content": note}]
    host.agent_loop(history, "Read the full background evidence")
    results = [block for message in history if message["role"] == "user"
               and isinstance(message["content"], list) for block in message["content"]
               if block.get("type") == "tool_result"]
    assert results[0]["content"] == "TAIL"
    assert not responses


def test_batch_save_failure_keeps_records_and_reuses_saved_path(host_factory, monkeypatch):
    host = host_factory()
    first = finish_task(host, monkeypatch, "a" * 250)
    second = finish_task(host, monkeypatch, "b" * 250)
    save = compaction.save_output
    calls = []

    def fail_second(output_id, output, **kwargs):
        calls.append(output_id)
        if len(calls) == 2:
            raise OSError("disk unavailable")
        return save(output_id, output, **kwargs)

    monkeypatch.setattr(compaction, "save_output", fail_second)
    with pytest.raises(OSError, match="disk unavailable"):
        scheduler.collect_background_results()
    assert set(scheduler.background_tasks) == {first, second}
    saved = scheduler.background_tasks[first]["saved_output_path"]
    notes = scheduler.collect_background_results()
    assert str(full_path(notes[0])) == saved
    assert len(calls) == 3
    assert calls[0] != calls[1] == calls[2]
    assert not scheduler.background_results


def test_two_collectors_deliver_each_result_once(host_factory, monkeypatch):
    host = host_factory()
    real_thread = threading.Thread
    finish_task(host, monkeypatch, "ready")
    # Restore actual thread construction for the executor after finishing the worker.
    monkeypatch.setattr(scheduler.threading, "Thread", real_thread)
    barrier = threading.Barrier(2)

    def collect():
        barrier.wait(timeout=5)
        return scheduler.collect_background_results()

    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = list(executor.map(lambda _: collect(), range(2)))
    assert sorted(map(len, (left, right))) == [0, 1]


def test_reused_bg_id_never_overwrites_old_output(host_factory, monkeypatch):
    host = host_factory()
    first = finish_task(host, monkeypatch, "a" * 250)
    old_path = full_path(scheduler.collect_background_results()[0])
    old_text = old_path.read_text()
    monkeypatch.setattr(scheduler, "_bg_counter", int(first.removeprefix("bg_")) - 1)
    second = finish_task(host, monkeypatch, "b" * 250)
    new_path = full_path(scheduler.collect_background_results()[0])
    assert first == second
    assert old_path != new_path
    assert old_path.read_text() == old_text


def test_notification_failure_keeps_sync_results_and_pending_batch(host_factory, monkeypatch):
    host = host_factory()
    finish_task(host, monkeypatch, "x" * 250)

    def fail_save(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(compaction, "save_output", fail_save)
    results = [{"type": "tool_result", "tool_use_id": "sync", "content": "sync result"}]
    assert host.build_user_content(results) == results
    history = [{"role": "user", "content": "go"}]
    LeadTurn(history).inject_background_notes()
    assert history == [{"role": "user", "content": "go"}]
    assert scheduler.has_pending_background()
