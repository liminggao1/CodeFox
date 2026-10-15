"""Exercise unified tools with real child lifetimes, directories and messages."""

import codefox.agents.subagent as subagent_runtime
import codefox.core as core
import codefox.registrations as registrations
import codefox.workspace.tasks as task_state
import codefox.workspace.worktrees as worktrees


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
def runtime(host_factory, monkeypatch, runtime_threads, no_approval):
    host = host_factory()
    original_thread = threading.Thread
    threads = runtime_threads
    def thread_factory(**kwargs):
        thread = original_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(subagent_runtime.threading, "Thread", thread_factory)
    yield host
    for name in list(subagent_runtime._active_subagents):
        subagent_runtime.run_request_shutdown(name).content
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive(), "Child did not shut down"


def client(host, create):
    core.client = SimpleNamespace(messages=SimpleNamespace(create=create))


def spawn(host, name="worker", mode="once", **kwargs):
    handler = registrations.BUILTIN_HANDLERS["spawn_subagent"]
    kwargs.setdefault("description", "Investigate")
    return json.loads(handler(name=name, prompt="Investigate", mode=mode, **kwargs).content)


def wait_event(host, kind, *, received=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        messages = subagent_runtime._BUS.wait_for_messages("lead", deadline - time.monotonic())
        if received is not None:
            received.extend(messages)
        error = next((m for m in messages if m["type"] == "error"), None)
        assert error is None, error
        found = next((m for m in messages if m["type"] == kind), None)
        if found:
            return found
    pytest.fail(f"No {kind} event")


def test_invalid_creation_has_no_side_effects(runtime):
    cases = [{"mode": "invalid"}, {"mode": []}, {"name": "../bad"}, {"name": "lead"},
             {"name": 3}, {"prompt": ""}, {"role": ""}, {"require_plan": "yes"},
             {"require_plan": True}, {"task_id": ""}, {"description": ""},
             {"description": None}, {"description": 1}, {"max_rounds": 0},
             {"max_rounds": True}, {"max_rounds": 1.5}, {"max_tool_calls": -1},
             {"max_tool_calls": False}, {"max_tool_calls": "2"}]
    for arguments in cases:
        host = runtime
        calls = []
        client(host, lambda **kwargs: calls.append(kwargs))
        request = dict(name="worker", description="Inspect", prompt="Inspect", mode="once")
        request.update(arguments)
        result = json.loads(registrations.run_spawn_subagent(**request).content)
        assert result["status"] == "failed"
        assert calls == []
        assert subagent_runtime._active_subagents == {}
        assert task_state.assignments == {}
        assert subagent_runtime._PROTOCOL.empty()


def test_once_round_limit_and_unfinished_task_are_failures(runtime, monkeypatch):
    host = runtime
    task = task_state.create_task("Unfinished")
    client(host, lambda **kwargs: blocks(text_block("Not finished")))
    result = spawn(host, task_id=task.id)
    assert result["status"] == "failed" and "unfinished" in result["reason"]
    assert result["text"] == "Not finished"
    assert task_state.load_task(task.id).status == "pending"
    assert task_state.assignments == {} and subagent_runtime._active_subagents == {}
    monkeypatch.setattr(core, "SUBAGENT_MAX_ROUNDS", 1)
    client(host, lambda **kwargs: blocks(tool_call("read_file", path="missing.txt")))
    result = spawn(host)
    assert result["status"] == "budget_exhausted"
    assert "model-call budget exhausted (1)" in result["error"]
    assert subagent_runtime._active_subagents == {}


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
    assert subagent_runtime._active_subagents == {} and subagent_runtime._PROTOCOL.empty()
    assert set(task_state.assignments) == {"agent"}


def test_persistent_without_task_keeps_history_and_never_auto_claims(runtime, monkeypatch):
    host = runtime
    calls = []
    waiting = threading.Event()
    original_wait = subagent_runtime._BUS.wait_for_messages
    def wait(name, timeout=None):
        if name == "worker":
            waiting.set()
        return original_wait(name, timeout)
    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", wait)
    def create(**kwargs):
        calls.append((id(subagent_runtime._active_subagents["worker"].messages),
                      list(kwargs["messages"])))
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
    assert subagent_runtime._active_subagents["worker"].active_request == "Second investigation"
    listing = json.loads(registrations.run_list_subagents().content)
    assert len(listing) == 1
    assert listing[0]["name"] == "worker"
    assert listing[0]["description"] == "Investigate"
    assert listing[0]["status"] == "idle"
    assert listing[0]["max_rounds"] == core.SUBAGENT_MAX_ROUNDS
    assert listing[0]["max_tool_calls"] == core.SUBAGENT_MAX_TOOL_CALLS
    assert listing[0]["agent_id"]
    duplicate = spawn(host, name="WORKER", mode="persistent")
    assert duplicate["status"] == "failed" and "already exists" in duplicate["text"]


def test_message_during_final_model_request_stays_in_current_work(runtime):
    host = runtime
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            first_started.set()
            assert release_first.wait(3)
            return blocks(text_block("First answer"))
        second_started.set()
        assert release_second.wait(3)
        return blocks(text_block("Answer with the update"))

    client(host, create)
    created = spawn(host, mode="persistent")
    assert created["status"] == "started"
    assert first_started.wait(3)
    try:
        subagent_runtime.send_message("lead", created["agent_id"], "Add one more requirement")
        release_first.set()
        assert second_started.wait(3)
        contexts = [message["content"] for message in requests[1]["messages"]
                    if isinstance(message["content"], str)
                    and "Current delegation:" in message["content"]]
        assert contexts
        context = contexts[-1]
        assert "Current delegation: Investigate" in context
        assert "Current delegation: Add one more requirement" not in context
        assert "Add one more requirement" in str(requests[1]["messages"])
    finally:
        release_first.set()
        release_second.set()

    result = subagent_runtime.get_subagent_output(created["agent_id"], timeout=3)
    delivered = []
    wait_event(host, "idle_notification", received=delivered)
    assert result["status"] == "completed"
    assert result["rounds"] == 2
    assert result["text"] == "Answer with the update"
    assert sum(message["type"] == "result" for message in delivered) == 1


def test_message_after_final_boundary_starts_next_work(runtime, monkeypatch):
    from codefox.agents.turn_engine import TurnEngine

    host = runtime
    boundary_entered = threading.Event()
    release_boundary = threading.Event()
    second_started = threading.Event()
    requests = []
    original_finish = TurnEngine._finish

    def pause_at_boundary(engine, status, reason="", *, response=None, error=None):
        if (status == "completed" and engine.callbacks.turn.name == "worker"
                and not boundary_entered.is_set()):
            boundary_entered.set()
            assert release_boundary.wait(3)
        return original_finish(engine, status, reason, response=response, error=error)

    monkeypatch.setattr(TurnEngine, "_finish", pause_at_boundary)

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            second_started.set()
        return blocks(text_block("First result" if len(requests) == 1 else "Follow-up result"))

    client(host, create)
    created = spawn(host, mode="persistent")
    assert created["status"] == "started"
    try:
        assert boundary_entered.wait(3)
        subagent_runtime.send_message("lead", created["agent_id"], "Follow-up")
    finally:
        release_boundary.set()

    assert second_started.wait(3)
    result = subagent_runtime.get_subagent_output(created["agent_id"], timeout=3)
    delivered = []
    wait_event(host, "idle_notification", received=delivered)
    context = [message["content"] for message in requests[1]["messages"]
               if isinstance(message["content"], str)
               and "Current delegation:" in message["content"]][-1]
    assert "Current delegation: Follow-up" in context
    assert result["status"] == "completed"
    assert result["rounds"] == 1
    assert result["text"] == "Follow-up result"
    assert sum(message["type"] == "result" for message in delivered) == 2
    assert sum(message["type"] == "idle_notification" for message in delivered) == 1


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
        histories.append(id(subagent_runtime._active_subagents["worker"].messages))
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
    assert len(set(histories)) == 1 and "worker" in subagent_runtime._active_subagents


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
        subagent_runtime._BUS.wait_for_messages("lead", deadline - time.monotonic())
    assert counts == {"receiver": 2, "sender": 2}
    assert "Additional evidence" in str(next(history for name, history in reversed(calls) if name == "sender"))
    assert subagent_runtime._active_subagents["sender"].active_request == "Investigate"
    assert "Peer investigation" in str(next(history for name, history in reversed(calls) if name == "receiver"))


def test_plan_wait_shutdown_records_stopped_engine_without_new_model_call(runtime, monkeypatch):
    from codefox.agents.turn_engine import TurnPhase
    sessions = []
    create_session = subagent_runtime.create_subagent_session

    def capture(*args, **kwargs):
        session = create_session(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(subagent_runtime, "create_subagent_session", capture)
    client(runtime, lambda **kwargs: blocks(tool_call("submit_plan", plan="Read source")))
    created = spawn(runtime, mode="persistent", require_plan=True)
    wait_event(runtime, "plan_approval_request")
    subagent_runtime.run_request_shutdown(created["agent_id"])
    stopped = wait_event(runtime, "stopped")
    assert stopped["metadata"]["reason"] == "Stopped by Lead"
    assert sessions[0].rounds == 1
    assert sessions[0].engine.phase is TurnPhase.STOPPED
    assert TurnPhase.WAITING_APPROVAL in sessions[0].engine.phase_history


@pytest.mark.parametrize("replay_approval,message_type", [
    (True, "plan_approval_response"), (False, "shutdown_request")])
def test_stale_protocol_does_not_wake_idle_model(runtime, monkeypatch, replay_approval, message_type):
    host = runtime
    histories = []
    delivered = threading.Event()
    waiting_again = threading.Event()
    wait = subagent_runtime._BUS.wait_for_messages

    def observe_wait(name, timeout=None):
        if name == "worker" and delivered.is_set():
            waiting_again.set()
        messages = wait(name, timeout)
        if name == "worker" and any(msg["content"] == "stale" for msg in messages):
            delivered.set()
        return messages

    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", observe_wait)

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
    subagent_runtime._BUS.send("lead", "worker", "stale", message_type,
                  {"request_id": request_id, "approve": True})
    assert waiting_again.wait(3)
    assert len(histories) == (2 if replay_approval else 1)
    registrations.run_send_message("worker", "next work").content
    wait_event(host, "idle_notification")
    assert len(histories) == (3 if replay_approval else 2)
    assert "next work" in str(histories[-1])
    assert "stale" not in str(histories[-1])


def test_persistent_budget_exhaustion_keeps_identity_for_the_next_work(runtime, monkeypatch):
    responses = iter([
        blocks(text_block("Partial finding"), tool_call("read_file", path="missing.txt")),
        blocks(text_block("Next work finished")),
    ])
    notifications = []
    original_send = subagent_runtime._BUS.send
    def send(*args, **kwargs):
        if len(args) > 3 and args[3] == "budget_exhausted":
            notifications.append(args[4])
        return original_send(*args, **kwargs)
    monkeypatch.setattr(subagent_runtime._BUS, "send", send)
    client(runtime, lambda **kwargs: next(responses))
    started = spawn(runtime, mode="persistent", description="Inspect source evidence",
                    max_rounds=1, tools=["read_file"])
    wait_event(runtime, "idle_notification")
    agent_id = started["agent_id"]
    result = subagent_runtime.get_subagent_output(agent_id, block=False)
    assert result["status"] == "budget_exhausted"
    assert result["text"] == "Partial finding"
    assert notifications == [result]
    assert subagent_runtime._active_subagents["worker"].status == "idle"
    subagent_runtime.send_message("lead", agent_id, "Do the next work")
    wait_event(runtime, "idle_notification")
    result = subagent_runtime.get_subagent_output(agent_id, block=False)
    assert result["status"] == "completed"
    assert result["text"] == "Next work finished"
    assert result["rounds"] == 1 and result["tool_calls"] == 0
    assert result["usage"]["model_calls"] == 2


def test_query_waits_for_current_work_and_does_not_repeat_notifications(runtime):
    calls = []
    started = threading.Event()
    release = threading.Event()

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            started.set()
            assert release.wait(3)
        return blocks(text_block(f"Finding {len(calls)}"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent", description="Inspect source evidence")
    wait_event(runtime, "idle_notification")
    agent_id = creation["agent_id"]
    original = subagent_runtime.get_subagent_output(agent_id, block=False)
    original_tokens = original["usage"]["tokens"]
    mutated = subagent_runtime.get_subagent_output(agent_id, block=False)
    mutated["usage"]["tokens"] = -1
    assert subagent_runtime.get_subagent_output(agent_id, block=False)["usage"]["tokens"] == original_tokens
    subagent_runtime.send_message("lead", agent_id, "Investigate again")
    assert started.wait(2)
    try:
        active = subagent_runtime.get_subagent_output(agent_id, block=False)
        assert active["status"] == "working"
        assert active["rounds"] == 1 and active["text"] == ""
        timed = subagent_runtime.get_subagent_output(agent_id, timeout=0)
        assert timed["status"] == "working" and timed["timed_out"] is True
    finally:
        release.set()
    finished = subagent_runtime.get_subagent_output(agent_id, timeout=2)
    wait_event(runtime, "idle_notification")
    assert finished["status"] == "completed" and finished["text"] == "Finding 2"
    assert not subagent_runtime.consume_lead_inbox()
    assert len(calls) == 2


def test_multiple_queued_messages_form_one_delegation(runtime, monkeypatch):
    calls = []
    received = threading.Event()
    release = threading.Event()
    wait = subagent_runtime._BUS.wait_for_messages

    def hold_messages(name, timeout=None):
        messages = wait(name, timeout)
        if name == "worker" and any(message["content"] == "Investigate A" for message in messages):
            received.set()
            assert release.wait(3)
        return messages

    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", hold_messages)

    def create(**kwargs):
        calls.append(kwargs)
        return blocks(text_block(f"Finding {len(calls)}"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent")
    wait_event(runtime, "idle_notification")
    try:
        subagent_runtime.send_message("lead", creation["agent_id"], "Investigate A")
        assert received.wait(2)
        subagent_runtime.send_message("lead", creation["agent_id"], "Investigate B")
    finally:
        release.set()
    finished = subagent_runtime.get_subagent_output(creation["agent_id"], timeout=2)
    assert finished["text"] == "Finding 2"
    context = next(message["content"] for message in reversed(calls[1]["messages"])
                   if isinstance(message["content"], str) and "Current delegation:" in message["content"])
    assert "Current delegation: Investigate A\n\nInvestigate B" in context
    assert finished["rounds"] == 1 and finished["usage"]["model_calls"] == 2
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["message", "plan_request"])
def test_new_delegation_invalidates_output_before_the_worker_receives_it(runtime, monkeypatch, kind):
    calls = []
    received = threading.Event()
    release = threading.Event()
    wait = subagent_runtime._BUS.wait_for_messages

    def hold_delegation(name, timeout=None):
        messages = wait(name, timeout)
        if name == "worker" and any(message["content"] == "Second investigation"
                                    for message in messages):
            received.set()
            assert release.wait(3)
        return messages

    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", hold_delegation)

    def create(**kwargs):
        calls.append(kwargs)
        return blocks(text_block(f"Finding {len(calls)}"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent")
    wait_event(runtime, "idle_notification")
    agent_id = creation["agent_id"]
    assert subagent_runtime.get_subagent_output(agent_id, block=False)["text"] == "Finding 1"
    try:
        if kind == "message":
            subagent_runtime.send_message("lead", agent_id, "Second investigation")
        else:
            assert subagent_runtime.run_request_plan(agent_id, "Second investigation").status == "success"
        assert received.wait(2)
        active = subagent_runtime.get_subagent_output(agent_id, block=False)
        assert active["status"] == "pending" and active["text"] == ""
        assert active["rounds"] == 0 and active["tool_calls"] == 0
        assert active["usage"]["model_calls"] == 1
        timed = subagent_runtime.get_subagent_output(agent_id, timeout=0.01)
        assert timed["status"] == "pending" and timed["timed_out"] is True
        assert len(calls) == 1
    finally:
        release.set()

    finished = subagent_runtime.get_subagent_output(agent_id, timeout=2)
    wait_event(runtime, "idle_notification")
    assert finished["status"] == "completed" and finished["text"] == "Finding 2"
    assert "Second investigation" in str(calls[1]["messages"])
    assert subagent_runtime._active_subagents["worker"].active_request == "Second investigation"


def test_failed_delivery_preserves_the_previous_output(runtime, monkeypatch):
    client(runtime, lambda **kwargs: blocks(text_block("Audit finished")))
    creation = spawn(runtime, mode="persistent")
    wait_event(runtime, "idle_notification")
    original = subagent_runtime.get_subagent_output(creation["agent_id"], block=False)
    send = subagent_runtime._BUS.send

    def fail_delivery(from_name, to, content, *args, **kwargs):
        if to == "worker" and content == "Next work":
            raise OSError("mailbox unavailable")
        return send(from_name, to, content, *args, **kwargs)

    monkeypatch.setattr(subagent_runtime._BUS, "send", fail_delivery)
    with pytest.raises(OSError, match="mailbox unavailable"):
        subagent_runtime.send_message("lead", creation["agent_id"], "Next work")
    assert subagent_runtime.get_subagent_output(creation["agent_id"], block=False) == original
    assert subagent_runtime.list_subagents()[0]["status"] == "idle"
