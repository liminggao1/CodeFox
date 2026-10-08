"""Exercise unified tools with real child lifetimes, directories and messages."""

import json
import threading
import time
from types import SimpleNamespace

import pytest


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool(name, **arguments):
    return SimpleNamespace(type="tool_use", id=f"{name}-{time.monotonic_ns()}",
                           name=name, input=arguments)


def response(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="end_turn")


@pytest.fixture
def runtime(host_factory, monkeypatch):
    host = host_factory()
    original_thread = threading.Thread
    threads = []
    def thread_factory(**kwargs):
        thread = original_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(host.subagent.threading, "Thread", thread_factory)
    yield host
    for name in list(host.active_subagents):
        host.run_request_shutdown(name)
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive(), "Child did not shut down"


def client(host, create):
    host.core.client = SimpleNamespace(messages=SimpleNamespace(create=create))


def spawn(host, name="worker", mode="once", **kwargs):
    handler = host.registrations.BUILTIN_HANDLERS["spawn_subagent"]
    return json.loads(handler(name=name, prompt="Investigate", mode=mode, **kwargs))


def wait_event(host, kind):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        messages = host.BUS.wait_for_messages("lead", deadline - time.monotonic())
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
    result = json.loads(host.run_spawn_subagent(**request))
    assert result["status"] == "failed"
    assert calls == []
    assert host.active_subagents == {}
    assert host.assignments == {}
    assert host.plan_gates == {}


def test_single_creation_tool_and_missing_mode(runtime):
    host = runtime
    tools, handlers = host.assemble_tool_pool()
    names = {item["name"] for item in tools}
    assert "spawn_subagent" in names and "list_subagents" in names
    assert not {"task", "spawn_teammate", "list_teammates"} & names
    result = host.call_tool_handler(handlers["spawn_subagent"],
                                   {"name": "worker", "prompt": "Inspect"}, "spawn_subagent")
    assert "Error" in result and host.active_subagents == {}


def test_once_round_limit_and_unfinished_task_are_failures(runtime, monkeypatch):
    host = runtime
    task = host.create_task("Unfinished")
    client(host, lambda **kwargs: response(text("Not finished")))
    result = spawn(host, task_id=task.id)
    assert result["status"] == "failed" and "unfinished" in result["text"]
    assert host.load_task(task.id).status == "pending"
    assert host.assignments == {} and host.active_subagents == {}
    monkeypatch.setattr(host.core, "SUBAGENT_MAX_ROUNDS", 1)
    client(host, lambda **kwargs: response(tool("read_file", path="missing.txt")))
    result = spawn(host)
    assert result["status"] == "failed" and "round limit" in result["text"]
    assert host.active_subagents == {}


def test_claim_and_thread_start_failures_roll_back(runtime, monkeypatch):
    host = runtime
    calls = []
    client(host, lambda **kwargs: calls.append(kwargs))
    task = host.create_task("Busy")
    host.claim_task(task.id, owner="agent")
    assert spawn(host, mode="persistent", task_id=task.id)["status"] == "failed"
    assert calls == [] and "agent" in host.assignments
    second = host.create_task("Start failure")
    def fail():
        raise RuntimeError("Thread start failed")
    monkeypatch.setattr(host.subagent.threading, "Thread",
                        lambda **kwargs: SimpleNamespace(start=fail))
    result = spawn(host, mode="persistent", task_id=second.id)
    assert result["status"] == "failed" and "start failed" in result["text"]
    assert host.load_task(second.id).status == "pending"
    assert host.active_subagents == {} and host.plan_gates == {}
    assert set(host.assignments) == {"agent"}


def test_once_uses_task_directory_until_final_answer(runtime, monkeypatch):
    host = runtime
    directory = host.core.WORKDIR / ".worktrees" / "once"
    directory.mkdir(parents=True)
    monkeypatch.setattr(host.worktrees, "_registered_worktree",
                        lambda name: (directory, None))
    task = host.create_task("Once edit")
    task.worktree = "once"
    host.save_task(task)
    responses = iter([
        response(tool("complete_task", task_id=task.id),
                 tool("write_file", path="once.txt", content="task result")),
        response(text("Completed")),
    ])
    client(host, lambda **kwargs: next(responses))
    result = spawn(host, task_id=task.id)
    assert result["status"] == "completed"
    assert directory.joinpath("once.txt").read_text() == "task result"
    assert not host.core.WORKDIR.joinpath("once.txt").exists()
    assert host.load_task(task.id).status == "completed"
    assert not host.assignments and not host.active_subagents


def test_persistent_without_task_keeps_history_and_never_auto_claims(runtime, monkeypatch):
    host = runtime
    calls = []
    waiting = threading.Event()
    original_wait = host.BUS.wait_for_messages
    def wait(name, timeout=None):
        if name == "worker":
            waiting.set()
        return original_wait(name, timeout)
    monkeypatch.setattr(host.BUS, "wait_for_messages", wait)
    def create(**kwargs):
        calls.append((id(kwargs["messages"]), list(kwargs["messages"])))
        return response(text(f"Result {len(calls)}"))
    client(host, create)
    assert spawn(host, mode="persistent")["status"] == "started"
    wait_event(host, "idle_notification")
    assert waiting.wait(1)
    task = host.create_task("Never auto claim")
    assert host.load_task(task.id).owner is None
    assert len(calls) == 1 and host.assignments == {}
    assert "Sent" in host.run_send_message("worker", "Second investigation")
    wait_event(host, "idle_notification")
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert any("Result 1" in str(message) for message in calls[1][1])
    assert host.active_subagents["worker"].active_request == "Second investigation"
    listing = json.loads(host.run_list_subagents())
    assert listing == [{"name": "worker", "role": "coder",
                        "mode": "persistent", "status": "idle"}]
    duplicate = spawn(host, name="WORKER", mode="persistent")
    assert duplicate["status"] == "failed" and "already exists" in duplicate["text"]


def test_persistent_switches_tasks_only_after_final_answer(runtime, monkeypatch):
    host = runtime
    directories = {}
    tasks = []
    for name in ("a", "b"):
        directory = host.core.WORKDIR / ".worktrees" / name
        directory.mkdir(parents=True)
        directories[name] = directory
        task = host.create_task(name)
        task.worktree = name
        host.save_task(task)
        tasks.append(task)
    monkeypatch.setattr(host.worktrees, "_registered_worktree",
                        lambda name: (directories[name], None))
    a, b = tasks
    responses = iter([
        response(tool("complete_task", task_id=a.id),
                 tool("write_file", path="same-batch.txt", content="a")),
        response(tool("write_file", path="next-round.txt", content="a")),
        response(text("A done")),
        response(tool("claim_task", task_id=b.id),
                 tool("write_file", path="b.txt", content="b"),
                 tool("complete_task", task_id=b.id)),
        response(text("B done")),
    ])
    histories = []
    def create(**kwargs):
        histories.append(id(kwargs["messages"]))
        return next(responses)
    client(host, create)
    spawn(host, mode="persistent", task_id=a.id)
    wait_event(host, "idle_notification")
    assert "worker" not in host.assignments
    assert directories["a"].joinpath("same-batch.txt").read_text() == "a"
    assert directories["a"].joinpath("next-round.txt").read_text() == "a"
    host.run_send_message("worker", f"Do Task {b.id}")
    wait_event(host, "idle_notification")
    assert directories["b"].joinpath("b.txt").read_text() == "b"
    assert not host.core.WORKDIR.joinpath("next-round.txt").exists()
    assert host.load_task(b.id).status == "completed"
    assert "worker" not in host.assignments
    assert len(set(histories)) == 1 and "worker" in host.active_subagents


def test_persistent_unfinished_task_retains_owner_until_guidance(runtime):
    host = runtime
    a, b = host.create_task("A"), host.create_task("B")
    calls = []
    responses = iter([response(text("Need guidance")),
                      response(tool("claim_task", task_id=b.id)),
                      response(tool("complete_task", task_id=a.id)),
                      response(text("A complete"))])
    def create(**kwargs):
        calls.append(list(kwargs["messages"]))
        return next(responses)
    client(host, create)
    spawn(host, mode="persistent", task_id=a.id)
    idle = wait_event(host, "idle_notification")
    assert "unfinished" in idle["content"]
    assert host.assignments["worker"]["task_id"] == a.id
    host.run_send_message("worker", "Finish A before moving on")
    wait_event(host, "idle_notification")
    assert "must finish" in calls[2][-1]["content"][0]["content"]
    assert host.load_task(b.id).status == "pending"
    assert host.assignments == {} and host.load_task(a.id).status == "completed"


def test_plan_approval_and_work_messages_use_real_protocol(runtime):
    host = runtime
    calls = []
    responses = iter([response(tool("submit_plan", plan="Read then write")),
                      response(tool("write_file", path="approved.txt", content="ok")),
                      response(text("Done"))])
    def create(**kwargs):
        calls.append(list(kwargs["messages"]))
        return next(responses)
    client(host, create)
    spawn(host, mode="persistent", require_plan=True)
    event = wait_event(host, "plan_approval_request")
    assert not host.core.WORKDIR.joinpath("approved.txt").exists()
    request_id = event["metadata"]["request_id"]
    assert "approved" in host.run_review_plan(request_id, True)
    wait_event(host, "idle_notification")
    assert host.core.WORKDIR.joinpath("approved.txt").read_text() == "ok"
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
            return response(text("Receiver ready" if counts[identity] == 1 else "Peer result"))
        if counts[identity] == 1:
            host.run_send_message("sender", "Additional evidence")
            return response(tool("send_message", to="receiver", content="Peer investigation"),
                            tool("send_message", to="lead", content="Progress"))
        return response(text("Sender done"))
    client(host, create)
    spawn(host, name="receiver", mode="persistent")
    wait_event(host, "idle_notification")
    spawn(host, name="sender", mode="persistent")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (counts.get("receiver", 0) < 2 or counts.get("sender", 0) < 2):
        host.BUS.wait_for_messages("lead", deadline - time.monotonic())
    assert counts == {"receiver": 2, "sender": 2}
    assert "Additional evidence" in str(next(history for name, history in reversed(calls) if name == "sender"))
    assert host.active_subagents["sender"].active_request == "Investigate"
    assert "Peer investigation" in str(next(history for name, history in reversed(calls) if name == "receiver"))
