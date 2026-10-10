"""Exercise unified tools with real child lifetimes, directories and messages."""

from agent_harness.runtime import tool_execution

import agent_harness.agents.subagent as subagent_runtime
import agent_harness.core as core
import agent_harness.registrations as registrations
import agent_harness.workspace.tasks as task_state
import agent_harness.tools.file_tools as file_tools
import agent_harness.workspace.worktrees as worktrees


import json
import threading
import time
from types import SimpleNamespace

import pytest

from support.fakes import blocks, text_block, tool_call


@pytest.fixture
def runtime_threads():
    return []


@pytest.fixture
def runtime(host_factory, monkeypatch, runtime_threads):
    host = host_factory()
    original_thread = threading.Thread
    threads = runtime_threads
    def thread_factory(**kwargs):
        thread = original_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(subagent_runtime.threading, "Thread", thread_factory)
    yield host
    for name in list(subagent_runtime.active_subagents):
        subagent_runtime.run_request_shutdown(name).content
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive(), "Child did not shut down"


def client(host, create):
    core.client = SimpleNamespace(messages=SimpleNamespace(create=create))


def spawn(host, name="worker", mode="once", **kwargs):
    handler = registrations.BUILTIN_HANDLERS["spawn_subagent"]
    return json.loads(handler(name=name, prompt="Investigate", mode=mode, **kwargs).content)


def wait_event(host, kind):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        messages = subagent_runtime.BUS.wait_for_messages("lead", deadline - time.monotonic())
        error = next((m for m in messages if m["type"] == "error"), None)
        assert error is None, error
        found = next((m for m in messages if m["type"] == kind), None)
        if found:
            return found
    pytest.fail(f"No {kind} event")


@pytest.mark.parametrize("arguments", [
    {"mode": "invalid"}, {"mode": []}, {"name": "../bad"}, {"name": "lead"},
    {"name": 3}, {"prompt": ""}, {"role": ""}, {"require_plan": "yes"},
    {"require_plan": True}, {"task_id": ""},
])
def test_invalid_creation_has_no_side_effects(runtime, arguments):
    host = runtime
    calls = []
    client(host, lambda **kwargs: calls.append(kwargs))
    request = dict(name="worker", prompt="Inspect", mode="once")
    request.update(arguments)
    result = json.loads(registrations.run_spawn_subagent(**request).content)
    assert result["status"] == "failed"
    assert calls == []
    assert subagent_runtime.active_subagents == {}
    assert task_state.assignments == {}
    assert subagent_runtime.PROTOCOL.empty()


def test_single_creation_tool_and_missing_mode(runtime):
    host = runtime
    tools, handlers = host.assemble_tool_pool()
    names = {item["name"] for item in tools}
    assert "spawn_subagent" in names and "list_subagents" in names
    assert not {"task", "spawn_teammate", "list_teammates"} & names
    result = tool_execution.call_tool_handler(handlers["spawn_subagent"],
                                   {"name": "worker", "prompt": "Inspect"}, "spawn_subagent").content
    assert "Error" in result and subagent_runtime.active_subagents == {}


def test_once_round_limit_and_unfinished_task_are_failures(runtime, monkeypatch):
    host = runtime
    task = task_state.create_task("Unfinished")
    client(host, lambda **kwargs: blocks(text_block("Not finished")))
    result = spawn(host, task_id=task.id)
    assert result["status"] == "failed" and "unfinished" in result["text"]
    assert task_state.load_task(task.id).status == "pending"
    assert task_state.assignments == {} and subagent_runtime.active_subagents == {}
    monkeypatch.setattr(core, "SUBAGENT_MAX_ROUNDS", 1)
    client(host, lambda **kwargs: blocks(tool_call("read_file", path="missing.txt")))
    result = spawn(host)
    assert result["status"] == "failed" and "round limit" in result["text"]
    assert subagent_runtime.active_subagents == {}


def test_claim_and_thread_start_failures_roll_back(runtime, monkeypatch):
    host = runtime
    calls = []
    client(host, lambda **kwargs: calls.append(kwargs))
    task = task_state.create_task("Busy")
    task_state.claim_task(task.id, owner="agent")
    assert spawn(host, mode="persistent", task_id=task.id)["status"] == "failed"
    assert calls == [] and "agent" in task_state.assignments
    second = task_state.create_task("Start failure")
    def fail():
        raise RuntimeError("Thread start failed")
    monkeypatch.setattr(subagent_runtime.threading, "Thread",
                        lambda **kwargs: SimpleNamespace(start=fail))
    result = spawn(host, mode="persistent", task_id=second.id)
    assert result["status"] == "failed" and "start failed" in result["text"]
    assert task_state.load_task(second.id).status == "pending"
    assert subagent_runtime.active_subagents == {} and subagent_runtime.PROTOCOL.empty()
    assert set(task_state.assignments) == {"agent"}


def test_once_uses_task_directory_until_final_answer(runtime, monkeypatch):
    host = runtime
    directory = core.WORKDIR / ".worktrees" / "once"
    directory.mkdir(parents=True)
    monkeypatch.setattr(worktrees, "_registered_worktree",
                        lambda name: (directory, None))
    task = task_state.create_task("Once edit")
    task.worktree = "once"
    task_state.save_task(task)
    responses = iter([
        blocks(tool_call("complete_task", task_id=task.id),
                 tool_call("write_file", path="once.txt", content="task result")),
        blocks(text_block("Completed")),
    ])
    client(host, lambda **kwargs: next(responses))
    result = spawn(host, task_id=task.id)
    assert result["status"] == "completed"
    assert directory.joinpath("once.txt").read_text() == "task result"
    assert not core.WORKDIR.joinpath("once.txt").exists()
    assert task_state.load_task(task.id).status == "completed"
    assert not task_state.assignments and not subagent_runtime.active_subagents


def test_persistent_without_task_keeps_history_and_never_auto_claims(runtime, monkeypatch):
    host = runtime
    calls = []
    waiting = threading.Event()
    original_wait = subagent_runtime.BUS.wait_for_messages
    def wait(name, timeout=None):
        if name == "worker":
            waiting.set()
        return original_wait(name, timeout)
    monkeypatch.setattr(subagent_runtime.BUS, "wait_for_messages", wait)
    def create(**kwargs):
        calls.append((id(kwargs["messages"]), list(kwargs["messages"])))
        return blocks(text_block(f"Result {len(calls)}"))
    client(host, create)
    assert spawn(host, mode="persistent")["status"] == "started"
    wait_event(host, "idle_notification")
    assert waiting.wait(1)
    task = task_state.create_task("Never auto claim")
    assert task_state.load_task(task.id).owner is None
    assert len(calls) == 1 and task_state.assignments == {}
    assert "Sent" in registrations.run_send_message("worker", "Second investigation").content
    wait_event(host, "idle_notification")
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert any("Result 1" in str(message) for message in calls[1][1])
    assert subagent_runtime.active_subagents["worker"].active_request == "Second investigation"
    listing = json.loads(registrations.run_list_subagents().content)
    assert listing == [{"name": "worker", "role": "coder",
                        "mode": "persistent", "status": "idle"}]
    duplicate = spawn(host, name="WORKER", mode="persistent")
    assert duplicate["status"] == "failed" and "already exists" in duplicate["text"]


def test_persistent_switches_tasks_only_after_final_answer(runtime, monkeypatch):
    host = runtime
    directories = {}
    tasks = []
    for name in ("a", "b"):
        directory = core.WORKDIR / ".worktrees" / name
        directory.mkdir(parents=True)
        directories[name] = directory
        task = task_state.create_task(name)
        task.worktree = name
        task_state.save_task(task)
        tasks.append(task)
    monkeypatch.setattr(worktrees, "_registered_worktree",
                        lambda name: (directories[name], None))
    a, b = tasks
    responses = iter([
        blocks(tool_call("complete_task", task_id=a.id),
                 tool_call("write_file", path="same-batch.txt", content="a")),
        blocks(tool_call("write_file", path="next-round.txt", content="a")),
        blocks(text_block("A done")),
        blocks(tool_call("claim_task", task_id=b.id),
                 tool_call("write_file", path="b.txt", content="b"),
                 tool_call("complete_task", task_id=b.id)),
        blocks(text_block("B done")),
    ])
    histories = []
    def create(**kwargs):
        histories.append(id(kwargs["messages"]))
        return next(responses)
    client(host, create)
    spawn(host, mode="persistent", task_id=a.id)
    wait_event(host, "idle_notification")
    assert "worker" not in task_state.assignments
    assert directories["a"].joinpath("same-batch.txt").read_text() == "a"
    assert directories["a"].joinpath("next-round.txt").read_text() == "a"
    registrations.run_send_message("worker", f"Do Task {b.id}").content
    wait_event(host, "idle_notification")
    assert directories["b"].joinpath("b.txt").read_text() == "b"
    assert not core.WORKDIR.joinpath("next-round.txt").exists()
    assert task_state.load_task(b.id).status == "completed"
    assert "worker" not in task_state.assignments
    assert len(set(histories)) == 1 and "worker" in subagent_runtime.active_subagents


def test_persistent_unfinished_task_retains_owner_until_guidance(runtime):
    host = runtime
    a, b = task_state.create_task("A"), task_state.create_task("B")
    calls = []
    responses = iter([blocks(text_block("Need guidance")),
                      blocks(tool_call("claim_task", task_id=b.id)),
                      blocks(tool_call("complete_task", task_id=a.id)),
                      blocks(text_block("A complete"))])
    def create(**kwargs):
        calls.append(list(kwargs["messages"]))
        return next(responses)
    client(host, create)
    spawn(host, mode="persistent", task_id=a.id)
    idle = wait_event(host, "idle_notification")
    assert "unfinished" in idle["content"]
    assert task_state.assignments["worker"]["task_id"] == a.id
    registrations.run_send_message("worker", "Finish A before moving on").content
    wait_event(host, "idle_notification")
    assert "must finish" in calls[2][-1]["content"][0]["content"]
    assert task_state.load_task(b.id).status == "pending"
    assert task_state.assignments == {} and task_state.load_task(a.id).status == "completed"


def test_plan_approval_and_work_messages_use_real_protocol(runtime):
    host = runtime
    calls = []
    responses = iter([blocks(tool_call("submit_plan", plan="Read then write")),
                      blocks(tool_call("write_file", path="approved.txt", content="ok")),
                      blocks(text_block("Done"))])
    def create(**kwargs):
        calls.append(list(kwargs["messages"]))
        return next(responses)
    client(host, create)
    spawn(host, mode="persistent", require_plan=True)
    event = wait_event(host, "plan_approval_request")
    assert not core.WORKDIR.joinpath("approved.txt").exists()
    request_id = event["metadata"]["request_id"]
    assert "approved" in subagent_runtime.run_review_plan(request_id, True).content
    wait_event(host, "idle_notification")
    assert core.WORKDIR.joinpath("approved.txt").read_text() == "ok"
    assert "[Plan approved]" in str(calls[1])


def test_messages_during_work_and_between_children_are_model_selected(runtime):
    host = runtime
    calls = []
    # Keep each child's response sequence independent of thread ordering.
    counts = {}
    def create(**kwargs):
        identity = "receiver" if "'receiver'" in kwargs["system"] else "sender"
        counts[identity] = counts.get(identity, 0) + 1
        calls.append((identity, list(kwargs["messages"])))
        if identity == "receiver":
            return blocks(text_block("Receiver ready" if counts[identity] == 1 else "Peer result"))
        if counts[identity] == 1:
            registrations.run_send_message("sender", "Additional evidence").content
            return blocks(tool_call("send_message", to="receiver", content="Peer investigation"),
                            tool_call("send_message", to="lead", content="Progress"))
        return blocks(text_block("Sender done"))
    client(host, create)
    spawn(host, name="receiver", mode="persistent")
    wait_event(host, "idle_notification")
    spawn(host, name="sender", mode="persistent")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (counts.get("receiver", 0) < 2 or counts.get("sender", 0) < 2):
        subagent_runtime.BUS.wait_for_messages("lead", deadline - time.monotonic())
    assert counts == {"receiver": 2, "sender": 2}
    assert "Additional evidence" in str(next(history for name, history in reversed(calls) if name == "sender"))
    assert subagent_runtime.active_subagents["sender"].active_request == "Investigate"
    assert "Peer investigation" in str(next(history for name, history in reversed(calls) if name == "receiver"))


def test_pending_plan_can_shutdown_without_another_model_call(runtime, runtime_threads):
    host = runtime
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return blocks(tool_call("submit_plan", plan="Read then change"))

    client(host, create)
    spawn(host, mode="persistent", require_plan=True)
    plan = wait_event(host, "plan_approval_request")
    assert "Shutdown requested" in subagent_runtime.run_request_shutdown("worker").content
    acknowledgement = wait_event(host, "shutdown_response")
    for thread in runtime_threads:
        thread.join(3)
        assert not thread.is_alive()
    assert "worker" not in subagent_runtime.active_subagents
    assert subagent_runtime.PROTOCOL.empty()
    assert len(calls) == 1
    assert acknowledgement["metadata"]["approve"] is True
    assert subagent_runtime.PROTOCOL.request(plan["metadata"]["request_id"]) is None


@pytest.mark.parametrize("replay_approval", [False, True])
@pytest.mark.parametrize("message_type", ["plan_approval_response", "shutdown_request"])
def test_stale_protocol_does_not_wake_idle_model(runtime, monkeypatch, replay_approval, message_type):
    host = runtime
    histories = []
    delivered = threading.Event()
    waiting_again = threading.Event()
    wait = subagent_runtime.BUS.wait_for_messages

    def observe_wait(name, timeout=None):
        if name == "worker" and delivered.is_set():
            waiting_again.set()
        messages = wait(name, timeout)
        if name == "worker" and any(msg["content"] == "stale" for msg in messages):
            delivered.set()
        return messages

    monkeypatch.setattr(subagent_runtime.BUS, "wait_for_messages", observe_wait)

    def create(**kwargs):
        histories.append(list(kwargs["messages"]))
        if replay_approval and len(histories) == 1:
            return blocks(tool_call("submit_plan", plan="Read first"))
        return blocks(text_block("Done"))

    client(host, create)
    spawn(host, mode="persistent", require_plan=replay_approval)
    request_id = "req_old"
    if replay_approval:
        request_id = wait_event(host, "plan_approval_request")["metadata"]["request_id"]
        assert "approved" in subagent_runtime.run_review_plan(request_id, True).content
    wait_event(host, "idle_notification")
    subagent_runtime.BUS.send("lead", "worker", "stale", message_type,
                  {"request_id": request_id, "approve": True})
    assert waiting_again.wait(3)
    assert len(histories) == (2 if replay_approval else 1)
    registrations.run_send_message("worker", "next work").content
    wait_event(host, "idle_notification")
    assert len(histories) == (3 if replay_approval else 2)
    assert "next work" in str(histories[-1])
    assert "stale" not in str(histories[-1])
