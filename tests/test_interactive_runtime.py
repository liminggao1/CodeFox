"""离线集成：真实序列化驱动与 host 主循环、Lead 状态拥有者和工具执行状态。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess
import threading

import pytest

from codefox.runtime import hooks
from codefox.tools import file_tools
from codefox.workspace import tasks
from support.fakes import fake_stream_for, response, tool
from support.helpers import displayed_approval, records, stored, until
import codefox.agents.subagent as subagent_runtime
import codefox.context.compaction as compaction
import codefox.context.memory_runtime as memory_runtime
import codefox.core as core
import codefox.runtime.scheduler as scheduler
import codefox.workspace.task_worktrees as task_worktrees


def add_display_messages(snapshot):
    for index, message in enumerate(snapshot["messages"]):
        snapshot["events"].append({"id": f"fixture-{index}", "at": snapshot["created_at"],
                                  "kind": "display", "type": "user_message",
                                  "source": "lead", "turn_id": "fixture",
                                  "payload": {"text": message["content"]}})


def stop_running_turn(runtime, started, release, submission):
    """等模型进入阻塞后请求停止当前回合，返回回合结束事件。"""
    assert started.wait(2)
    started_event, _ = until(runtime, "turn_started")
    assert runtime.request_stop(started_event.turn_id)
    release.set()
    return until(runtime, "turn_finished", submission_id=submission.submission_id)


def test_serial_submission_history_events_and_usage(runtime_factory):
    started, release = threading.Event(), threading.Event()
    def create(**kwargs):
        started.set()
        assert release.wait(3)
        return response("**你好**")
    runtime, host = runtime_factory(client=create)
    submission = runtime.submit("hello")
    assert started.wait(2)
    assert runtime.submit("second").status == "busy"
    release.set()
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert finished.payload["saved"]
    assert [event.payload["text"] for event in events if event.type == "assistant_message"] == ["**你好**"]
    snapshot = [event.payload for event in events if event.type == "state_snapshot"][-1]
    assert snapshot["lead_main_model_usage"] == {"input_tokens": 10, "output_tokens": 3, "model_calls": 1}
    saved = runtime.session.store.load(runtime.session.session_id)
    assert saved["messages"][0]["content"] == "hello"
    assert saved["messages"][-1]["content"][0]["text"] == "**你好**"
    assert not runtime.request_stop(finished.turn_id)
    assert runtime.submit("/goal").status == "accepted"


def test_stop_waiting_approval_completes_tools_and_pauses_background(runtime_factory):
    runtime, host = runtime_factory([
        response(tools=[tool("bash", command="touch unwanted"),
                        tool("write_file", "write", path="unwanted", content="x")], stop="tool_use"),
        response("Next"),
    ])
    submission = runtime.submit("run")
    request, _ = displayed_approval(runtime)
    assert request is not None
    assert runtime.request_stop(request.turn_id)
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert not (core.WORKDIR / "unwanted").exists()
    assert len([event for event in events if event.type == "tool_result"]) == 2
    decisions = records(stored(runtime), "approval_decided")
    assert len(decisions) == 1 and decisions[0]["payload"]["decision"] == "deny"
    subagent_runtime._BUS.send("child", "lead", "Waiting result")
    assert runtime.turns.background_paused
    assert runtime.submit("continue").status == "accepted"
    assert not runtime.turns.background_paused


def test_close_waiting_approval_is_nonblocking_and_terminal(runtime_factory):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="touch unwanted")], stop="tool_use")])
    runtime.submit("run")
    request, _ = displayed_approval(runtime)
    assert request
    runtime.close()
    runtime.close()
    assert runtime.submit("new").status == "closed"
    closed, events = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    assert len([event for event in events if event.type == "turn_finished"]) == 1
    assert not (core.WORKDIR / "unwanted").exists()
    decisions = records(stored(runtime), "approval_decided")
    assert len(decisions) == 1 and decisions[0]["payload"]["decision"] == "closed"


def test_save_failure_does_not_replace_success_outcome(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    def fail(snapshot):
        raise OSError("disk unavailable")
    monkeypatch.setattr(runtime.session.store, "save", fail)
    submission = runtime.submit("run")
    finished, events = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert not finished.payload["saved"]
    assert any(event.type == "session_save_error" for event in events)
    session_id, generation = runtime.session.session_id, runtime._generation
    history = list(runtime.session.history)
    submission = runtime.submit("/new")
    command, events = until(runtime, "command_result", submission_id=submission.submission_id)
    assert command.payload["status"] == "failed"
    assert "未开启新会话" in command.payload["data"]["error"]
    assert runtime.session.session_id == session_id and runtime._generation == generation
    assert runtime.session.history == history
    assert not any(event.type == "session_changed" for event in events)
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert not closed.payload["saved"]


def test_new_command_saves_old_session_and_resets_context(runtime_factory):
    runtime, host = runtime_factory()
    submission = runtime.submit("first")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    previous = runtime.session.session_id
    host.GOAL.set_goal("keep working")
    submission = runtime.submit("/new")
    command, events = until(runtime, "command_result", submission_id=submission.submission_id)
    assert command.payload["status"] == "completed"
    assert runtime.session.session_id != previous
    assert runtime.session.history == []
    assert not host.GOAL.snapshot()["active"]
    assert len([event for event in events if event.type == "session_changed"]) == 1
    # 旧会话仍可恢复，且保存了切换前的消息。
    saved = runtime.session.store.load(previous)
    assert saved["messages"][0]["content"] == "first"


def test_cli_owns_all_reads_and_waits_for_goal_followup(runtime_factory, monkeypatch):
    from codefox.frontends.cli import CLIAdapter
    runtime, host = runtime_factory([response(tools=[tool("bash", command="printf hello")], stop="tool_use"), response("After")])
    monkeypatch.setattr(host, "evaluate_goal", lambda messages: None)
    answers = iter(["/goal do it", "yes", "q"])
    reads, output = [], []
    def reader(prompt=None):
        reads.append((threading.get_ident(), prompt))
        return next(answers)
    CLIAdapter(runtime, reader, output.append).run()
    assert {ident for ident, _ in reads} == {threading.get_ident()}
    assert reads[1][1] == "Allow? [y/N] "
    assert sum(text.endswith("After") for text in output) == 1
    assert "hello" in str(output)


def test_stop_model_context_error_does_not_start_reactive_summary(runtime_factory, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls, summaries = [], []
    def create(**kwargs):
        calls.append(True)
        started.set()
        assert release.wait(3)
        raise RuntimeError("context_length_exceeded")
    runtime, host = runtime_factory(client=create)
    monkeypatch.setattr(compaction, "summarize_history",
                        lambda *args, **kwargs: summaries.append(True) or "summary")
    job = scheduler.schedule_job("0 0 1 1 *", "deliver once", recurring=False)
    scheduler._enqueue_due_job(job)
    submission = runtime.submit("run")
    assert started.wait(2)
    turn, _ = until(runtime, "turn_started")
    assert runtime.request_stop(turn.turn_id)
    release.set()
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    # Stop must prevent new recovery work without concealing the model error.
    assert finished.payload["outcome"]["status"] == "failed"
    assert "context_length_exceeded" in finished.payload["outcome"]["reason"]
    assert calls == [True] and summaries == []
    assert scheduler._cron_queue == [job]
    assert scheduler._scheduled_jobs[job.id].pending_delivery


def test_stop_during_real_memory_extraction_preserves_writes_without_consolidation(runtime_factory, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return response("Preference noted")
        started.set()
        assert release.wait(3)
        return response('[{"name":"language","type":"user","scope":"persistent",'
                        '"description":"Preferred language","body":"Use Chinese in future sessions."}]')
    runtime, host = runtime_factory(client=create, enable_memory=True)
    consolidated = []
    monkeypatch.setattr(memory_runtime, "consolidate_memories", lambda: consolidated.append(True))
    submission = runtime.submit("Please remember to use Chinese in future sessions")
    finished, _ = stop_running_turn(runtime, started, release, submission)
    assert finished.payload["outcome"]["status"] == "stopped"
    assert len(calls) == 2
    assert not consolidated
    assert memory_runtime.list_memory_files()[0]["name"] == "language"


def test_background_delivery_runs_with_noninteractive_permissions(runtime_factory):
    runtime, host = runtime_factory([response("initial"), response(tools=[tool("bash", command="touch unwanted")], stop="tool_use"), response("delivered")])
    submission = runtime.submit("run")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    subagent_runtime._BUS.send("child", "lead", "Background result")
    finished, events = until(runtime, "turn_finished")
    assert finished.payload["submission_id"] is None
    assert finished.payload["outcome"]["status"] == "completed"
    assert runtime.channel.next_approval() is None
    assert not (core.WORKDIR / "unwanted").exists()
    assert "Background result" in str(runtime.session.history)
    assert "execution context" in str([event.payload for event in events if event.type == "tool_result"])


def test_frontend_once_inherits_approval_and_workflow_does_not(runtime_factory):
    from codefox.workflows import workflow_runtime
    child_call = tool("spawn_subagent", name="helper", description="Execute child work", prompt="execute", mode="once")
    runtime, host = runtime_factory([response(tools=[child_call], stop="tool_use"),
                                     response(tools=[tool("bash", command="printf child")], stop="tool_use"),
                                     response("child done"), response("lead done")])
    submission = runtime.submit("run")
    request, _ = displayed_approval(runtime)
    assert request and request.source == "helper"
    assert runtime.channel.respond(request.request_id, request.generation, "approve")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"

    from codefox.runtime.interaction import ExecutionContext, execution_context
    values = iter([response(tools=[tool("bash", command="touch unwanted")], stop="tool_use"), response("workflow done")])
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: next(values)))
    context = ExecutionContext(runtime.channel, runtime.session.session_id, 1, "frontend", origin="frontend")
    with execution_context(context):
        result = workflow_runtime.SubagentRunner(client=client).run("work")
    assert result.value == "workflow done"
    assert runtime.channel.next_approval() is None
    assert not (core.WORKDIR / "unwanted").exists()


def test_close_waits_for_owned_persistent_thread(runtime_factory):
    child_call = tool("spawn_subagent", name="helper", description="Execute child work", prompt="work", mode="persistent")
    def create(**kwargs):
        if "You are 'helper'" in str(kwargs.get("system")):
            return response("child done")
        if len(kwargs["messages"]) == 2:
            return response(tools=[child_call], stop="tool_use")
        return response("lead done")
    runtime, host = runtime_factory(client=create)
    submission = runtime.submit("run")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    runtime.close()
    closed, _ = until(runtime, "runtime_closed")
    assert closed.payload["saved"]
    assert not subagent_runtime._active_subagents
    assert not subagent_runtime._persistent_threads


@pytest.fixture
def session_worktree(runtime_factory):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    root = core.WORKDIR
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=Test", "-c",
                               "user.email=test@example.invalid", *args], cwd=root,
                              check=True, capture_output=True, text=True)
    git("init", "-q")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "initial")
    submission = runtime.submit("session A")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    with host.agent_lock:
        task = tasks.create_task("session A task")
        assert task_worktrees.create_worktree("feature", task.id).startswith("Worktree")
        assert tasks.claim_task(task.id).startswith("Claimed")
        file_tools.run_todo_write([{"content": "验证中文目录", "status": "pending"}]).content
        runtime._checkpoint()
    return runtime, host, task, root / ".worktrees" / "feature"


def resume_command(runtime, session_id):
    submission = runtime.submit(f"/resume {session_id}")
    assert submission.status == "accepted"
    return until(runtime, "command_result", submission_id=submission.submission_id)


def test_resume_target_save_failure_stays_target_and_retries(runtime_factory, monkeypatch):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    submission = runtime.submit("original")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    target = runtime.session.store.create()
    target.update(goal=host.GOAL.snapshot(), messages=[{"role": "user", "content": "target"}])
    add_display_messages(target)
    runtime.session.store.save(target)
    save = runtime.session.store.save
    def unavailable(snapshot):
        if snapshot["id"] == target["id"]:
            raise OSError("disk full")
        save(snapshot)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.session.store, "save", unavailable)
        result, events = resume_command(runtime, target["id"])
        assert result.payload["status"] == "completed"
        assert result.payload["data"]["saved"] is False
        assert "上下文未能保存" in result.payload["data"]["text"]
        assert any(event.type == "session_save_error" for event in events)
        assert runtime.session.session_id == target["id"] and runtime.session.dirty
        assert runtime.session.history == target["messages"]
    submission = runtime.submit("continue target")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["saved"] and not runtime.session.dirty
    saved = runtime.session.store.load(target["id"])
    assert any(event["type"] == "resume" for event in saved["events"])
    assert saved["messages"][1]["content"] == "continue target"


def test_resume_mailbox_error_delivers_target_state_and_command_end(runtime_factory):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    submission = runtime.submit("original")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    root = core.WORKDIR / "target"
    root.mkdir()
    mailbox = root / ".mailboxes" / "lead.jsonl"
    mailbox.parent.mkdir()
    mailbox.write_text("invalid json\n", encoding="utf-8")
    target = runtime.session.store.create()
    target.update(workdir=str(root), goal=host.GOAL.snapshot(),
                  messages=[{"role": "user", "content": "target history"}])
    add_display_messages(target)
    runtime.session.store.save(target)
    result, events = resume_command(runtime, target["id"])
    assert result.payload["status"] == "failed"
    assert "切换后的处理失败" in result.payload["data"]["error"]
    assert runtime.session.session_id == target["id"]
    assert core.WORKDIR == root and Path.cwd() == root
    changed = next(event for event in events if event.type == "session_changed")
    state = [event for event in events if event.type == "state_snapshot"][-1]
    assert changed.payload["records"][0]["payload"]["text"] == "target history"
    assert result.generation == changed.generation == state.generation == 2
    assert state.payload["busy"] is False
    assert scheduler._runtime_thread.is_alive()
    # Correct the damaged input, then continue the already restored session.
    mailbox.write_text("", encoding="utf-8")
    submission = runtime.submit("continue target")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert runtime.session.store.load(target["id"])["messages"][1]["content"] == "continue target"


def test_resume_isolates_task_directory_and_todos(session_worktree):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    saved = runtime.session.store.load(a)
    assert saved["task_id"] == task.id
    assert saved["worktree"] == "feature"
    assert saved["todos"][0]["content"] == "验证中文目录"
    assert tasks.load_task(task.id).session_id == a
    b = runtime.session.store.create()
    b["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(b)
    result, _ = resume_command(runtime, b["id"])
    assert result.payload["status"] == "completed", result.payload
    assert tasks.assignment_cwd("agent") == core.WORKDIR
    assert not runtime.lead_context.todos
    with host.agent_lock:
        with pytest.raises(ValueError, match="another session"):
            tasks.complete_task(task.id)
        with pytest.raises(ValueError, match="cannot claim"):
            tasks.claim_task(task.id)
        other = tasks.create_task("session B task")
        assert tasks.claim_task(other.id).startswith("Claimed")
    result, events = resume_command(runtime, a)
    assert result.payload["status"] == "completed"
    assert tasks.assignment_cwd("agent") == checkout
    assert runtime.lead_context.todos == saved["todos"]
    assert tasks.load_task(other.id).status == "in_progress"
    display = [e for e in events if e.type == "state_snapshot"][-1]
    assert display.payload["todos"] == saved["todos"]
    assert file_tools.run_agent_write("resumed.txt", "continued").content.startswith("Wrote")
    assert (checkout / "resumed.txt").read_text() == "continued"
    assert not (core.WORKDIR / "resumed.txt").exists()


def test_cli_display_failure_closes_pending_approval(runtime_factory):
    from codefox.frontends.cli import CLIAdapter
    runtime, host = runtime_factory([response("Before", tools=[tool("bash", command="touch unwanted")], stop="tool_use")])
    reads = []
    def reader(prompt=None):
        reads.append(prompt)
        return "run"
    def write(text):
        if text.endswith("Before"):
            raise OSError("terminal detached")
    CLIAdapter(runtime, reader, write).run()
    assert reads == [None]
    assert not (core.WORKDIR / "unwanted").exists()
    assert runtime.submit("next").status == "closed"


@pytest.mark.parametrize("change", ["missing_task", "owner", "binding", "directory"])
def test_resume_rejects_invalid_association_without_changing_current(session_worktree, change):
    runtime, host, task, checkout = session_worktree
    a = runtime.session.session_id
    b = runtime.session.store.create()
    b["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(b)
    switched, _ = resume_command(runtime, b["id"])
    assert switched.payload["status"] == "completed", switched.payload
    if change == "missing_task":
        tasks._task_path(task.id).unlink()
    elif change == "directory":
        checkout.rename(checkout.with_name("moved"))
    else:
        saved_task = tasks.load_task(task.id)
        if change == "owner":
            saved_task.owner = "other"
        else:
            saved_task.worktree = None
        tasks.save_task(saved_task)
    before = list(runtime.session.history)
    result, _ = resume_command(runtime, a)
    assert result.payload["status"] == "failed"
    assert runtime.session.session_id == b["id"]
    assert runtime.session.history == before
    assert runtime.lead_context.session_id == b["id"]
    assert tasks.assignment_cwd("agent") == core.WORKDIR


def test_model_claim_and_todo_are_saved_before_next_request(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    task = tasks.create_task("model task")
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return response(tools=[tool("claim_task", "claim", task_id=task.id),
                                   tool("todo_write", "todo", todos=[{"content": "verify", "status": "in_progress"}])],
                            stop="tool_use")
        saved = runtime.session.store.load(runtime.session.session_id)
        assert saved["task_id"] == task.id
        assert saved["todos"] == [{"content": "verify", "status": "in_progress"}]
        assert tasks.load_task(task.id).session_id == runtime.session.session_id
        return response("Task retained")
    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create, stream=fake_stream_for(create))))
    submission = runtime.submit("claim and track task")
    finished, _ = until(runtime, "turn_finished", submission_id=submission.submission_id)
    assert finished.payload["outcome"]["status"] == "completed"
    assert len(calls) == 2


def test_switch_directory_reloads_cron_and_keeps_each_session_root(runtime_factory):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    original_root = core.WORKDIR
    submission = runtime.submit("original session")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    original_id = runtime.session.session_id
    original_job = scheduler.schedule_job("0 0 1 1 *", "original", durable=True)
    transient_job = scheduler.schedule_job("0 0 1 1 *", "transient", durable=False)

    other_root = original_root / "other"
    other_root.mkdir()
    other = runtime.session.store.create()
    other.update(workdir=str(other_root), goal=host.GOAL.snapshot())
    runtime.session.store.save(other)
    result, _ = resume_command(runtime, other["id"])
    assert result.payload["status"] == "completed", result.payload
    assert Path.cwd() == core.WORKDIR == other_root
    assert not scheduler._scheduled_jobs
    assert tasks.assignment_cwd("agent") == other_root
    assert file_tools.run_agent_write("here.txt", "other").content.startswith("Wrote")
    assert (other_root / "here.txt").read_text() == "other"
    other_job = scheduler.schedule_job("0 0 1 1 *", "other", durable=True)

    result, _ = resume_command(runtime, original_id)
    assert result.payload["status"] == "completed", result.payload
    assert Path.cwd() == core.WORKDIR == original_root
    assert set(scheduler._scheduled_jobs) == {original_job.id}
    assert transient_job.id not in scheduler._scheduled_jobs
    assert runtime.session.store.load(original_id)["workdir"] == str(original_root)
    assert runtime.session.store.load(other["id"])["workdir"] == str(other_root)
    result, _ = resume_command(runtime, other["id"])
    assert result.payload["status"] == "completed", result.payload
    assert set(scheduler._scheduled_jobs) == {other_job.id}


@pytest.mark.parametrize("failure", ["save", "chdir", "queued_cron", "missing_directory"])
def test_cross_directory_failure_preserves_current_context(runtime_factory, monkeypatch, failure):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    original_root = core.WORKDIR
    submission = runtime.submit("keep current")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    current_id = runtime.session.session_id
    other_root = original_root / "other"
    other_root.mkdir()
    target = runtime.session.store.create()
    target_root = other_root / "missing" if failure == "missing_directory" else other_root
    target.update(workdir=str(target_root), goal=host.GOAL.snapshot())
    runtime.session.store.save(target)
    job = scheduler.schedule_job("0 0 1 1 *", "keep scheduler", durable=True)
    if failure == "queued_cron":
        with scheduler._cron_lock:
            scheduler._enqueue_due_job(job)
        runtime.turns.pause_background()
    def fail(*args):
        raise OSError("unavailable")
    if failure == "save":
        monkeypatch.setattr(runtime.session.store, "save", fail)
    elif failure == "chdir":
        monkeypatch.setattr("codefox.runtime.session_switch.os.chdir", fail)
    before_history = list(runtime.session.history)
    result, _ = resume_command(runtime, target["id"])
    assert result.payload["status"] == "failed", result.payload
    assert runtime.session.session_id == current_id
    assert runtime.session.history == before_history
    assert Path.cwd() == core.WORKDIR == runtime.session.store.workdir == original_root
    assert set(scheduler._scheduled_jobs) == {job.id}
    assert scheduler._runtime_thread.is_alive()


@pytest.mark.parametrize("command", ["/new", "/resume"])
def test_session_activation_rechecks_cron_after_save(runtime_factory, monkeypatch, command):
    from codefox.runtime.session_store import SessionError

    runtime, host = runtime_factory(client=lambda **kwargs: response())
    submission = runtime.submit("keep current")
    until(runtime, "turn_finished", submission_id=submission.submission_id)
    switch = runtime.session_switch
    current_id = runtime.session.session_id
    current_goal = host.GOAL
    generation = runtime._generation
    root = core.WORKDIR
    history = list(runtime.session.history)
    target = runtime.session.store.create()
    target.update(goal=host.GOAL.snapshot())
    runtime.session.store.save(target)
    job = scheduler.schedule_job("0 0 1 1 *", "arrived while saving", recurring=False)
    checkpoint = switch._checkpoint

    def save_then_enqueue():
        saved = checkpoint()
        with scheduler._cron_lock:
            scheduler._enqueue_due_job(job)
        return saved

    monkeypatch.setattr(switch, "_checkpoint", save_then_enqueue)
    with host.agent_lock:
        runtime.turns.pause_background()
        with pytest.raises(SessionError, match=f"计划任务结果未处理.*{command}"):
            if command == "/new":
                switch.start_new()
            else:
                switch.switch(target["id"])
        assert runtime.session.session_id == current_id
        assert runtime.session.history == history
        assert host.GOAL is current_goal
        assert runtime._generation == generation
        assert Path.cwd() == core.WORKDIR == root
        assert scheduler.pending_cron_prompts() == (job.prompt,)


def test_post_hook_failure_saves_real_result_and_pairs_unexecuted_calls(runtime_factory, monkeypatch, no_approval):
    runtime, _ = runtime_factory([response(tools=[
        tool("write_file", "first", path="first.txt", content="written"),
        tool("write_file", "second", path="second.txt", content="never")], stop="tool_use")])

    def fail(*args):
        raise RuntimeError("post failed")

    monkeypatch.setitem(hooks.HOOKS, "PostToolUse", [fail])
    submitted = runtime.submit("write")
    finished, events = until(runtime, "turn_finished", submission_id=submitted.submission_id)
    snapshot = stored(runtime)
    assert finished.payload["outcome"]["status"] == "failed"
    results = snapshot["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "first" and not results[0].get("is_error")
    assert results[1]["tool_use_id"] == "second" and results[1]["is_error"]
    assert (runtime.session.store.workdir / "first.txt").read_text() == "written"
    assert not (runtime.session.store.workdir / "second.txt").exists()
    assert any(event.type == "log" and "[HOOK] PostToolUse" in event.payload["text"]
               and "失败" in event.payload["text"] and "RuntimeError: post failed" in event.payload["text"]
               for event in events)
    assert not any(item["payload"].get("phase") == "hook"
                   for item in records(snapshot, "execution_status"))
    restored = runtime.session.store.load(snapshot["id"])
    assert restored["messages"] == snapshot["messages"]


# -- C2：命令路由是独立 module，命令结果经明确对象返回，不再走实例字段通道 --

def test_command_dispatch_returns_explicit_outcome(runtime_factory):
    runtime, host = runtime_factory(client=lambda **kwargs: response())
    router = runtime.command_router

    goal_outcome = router.dispatch("/goal ship the feature")
    assert goal_outcome.command == "/goal"
    assert goal_outcome.data["text"] == "已设置目标：ship the feature"
    # /goal <text> 之后仍要跑一个回合，followup 是明确返回值而不是隐藏字段。
    assert goal_outcome.followup == "ship the feature"

    query_outcome = router.dispatch("/goal")
    assert query_outcome.command == "/goal"
    assert "ship" in query_outcome.data["text"]
    assert query_outcome.followup is None
    assert "sessions" in router.dispatch("/resume").data
    from codefox.runtime.session_store import SessionError
    with pytest.raises(SessionError):
        router.dispatch("/delete")
