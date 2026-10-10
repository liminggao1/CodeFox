"""Full background output is retained before a batch is collected once."""

import agent_harness.runtime.scheduler as scheduler
import agent_harness.tools.file_tools as file_tools
import agent_harness.runtime.hooks as hooks
import agent_harness.core as core
import agent_harness.registrations as registrations
import agent_harness.workspace.worktrees as worktrees
import agent_harness.workspace.tasks as tasks
import agent_harness.context.compaction as compaction


from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest


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
    host.inject_background_notifications(history)
    assert history == [{"role": "user", "content": "go"}]
    assert scheduler.has_pending_background()
