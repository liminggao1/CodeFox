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


@pytest.mark.parametrize("arguments", [
    {"mode": "invalid"}, {"mode": []}, {"name": "../bad"}, {"name": "lead"},
    {"name": 3}, {"prompt": ""}, {"role": ""}, {"require_plan": "yes"},
    {"require_plan": True}, {"task_id": ""},
])
def test_invalid_creation_has_no_side_effects(runtime, arguments):
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


def test_single_creation_tool_and_missing_mode(runtime):
    host = runtime
    tools, handlers = host.assemble_tool_pool()
    names = {item["name"] for item in tools}
    assert "spawn_subagent" in names and "list_subagents" in names
    assert not {"task", "spawn_teammate", "list_teammates"} & names
    result = tool_execution.call_tool_handler(handlers["spawn_subagent"],
                                   {"name": "worker", "prompt": "Inspect"}, "spawn_subagent").content
    assert "Error" in result and subagent_runtime._active_subagents == {}


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
    assert not task_state.assignments and not subagent_runtime._active_subagents


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
        subagent_runtime._BUS.wait_for_messages("lead", deadline - time.monotonic())
    assert counts == {"receiver": 2, "sender": 2}
    assert "Additional evidence" in str(next(history for name, history in reversed(calls) if name == "sender"))
    assert subagent_runtime._active_subagents["sender"].active_request == "Investigate"
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
    assert "worker" not in subagent_runtime._active_subagents
    assert subagent_runtime._PROTOCOL.empty()
    assert len(calls) == 1
    assert acknowledgement["metadata"]["approve"] is True
    assert subagent_runtime._PROTOCOL.request(plan["metadata"]["request_id"]) is None


@pytest.mark.parametrize("replay_approval", [False, True])
@pytest.mark.parametrize("message_type", ["plan_approval_response", "shutdown_request"])
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


def test_stopped_notification_preserves_the_result_and_terminal_reason(runtime, runtime_threads):
    client(runtime, lambda **kwargs: blocks(text_block("Audit finished")))
    started = spawn(runtime, mode="persistent", description="Inspect source evidence")
    wait_event(runtime, "idle_notification")
    agent_id = started["agent_id"]
    assert subagent_runtime.run_request_shutdown(agent_id).status == "success"
    stopped = wait_event(runtime, "stopped")
    for thread in runtime_threads:
        thread.join(2)
    output = subagent_runtime.get_subagent_output(agent_id, block=False)
    assert stopped["metadata"] == output
    assert output["status"] == "stopped"
    assert output["text"] == "Audit finished"
    assert "Stopped by Lead" in output["reason"]
    assert not subagent_runtime._active_subagents


def test_model_budget_exhaustion_does_not_wait_for_plan_approval(runtime):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return blocks(tool_call("submit_plan", plan="Read the source"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent", require_plan=True,
                     tools=["submit_plan"], max_rounds=1, max_tool_calls=2)
    result = subagent_runtime.get_subagent_output(creation["agent_id"], timeout=1)

    assert result["status"] == "budget_exhausted"
    assert result["rounds"] == 1 and result["tool_calls"] == 1
    assert "model-call" in result["reason"]
    events = []
    wait_event(runtime, "idle_notification", received=events)
    assert next(message["metadata"] for message in events
                if message["type"] == "budget_exhausted") == result
    assert subagent_runtime.list_subagents()[0]["status"] == "idle"
    assert len(calls) == 1


def test_approval_of_exhausted_work_does_not_reset_its_budget(runtime, monkeypatch):
    calls = []
    approved = threading.Event()
    waiting_again = threading.Event()
    wait = subagent_runtime._BUS.wait_for_messages

    def observe_wait(name, timeout=None):
        if name == "worker" and approved.is_set():
            waiting_again.set()
        messages = wait(name, timeout)
        if name == "worker" and any(message["type"] == "plan_approval_response"
                                    for message in messages):
            approved.set()
        return messages

    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", observe_wait)
    core.WORKDIR.joinpath("source.txt").write_text("Current evidence", encoding="utf-8")
    task = task_state.create_task("Inspect source")

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return blocks(tool_call("submit_plan", plan="Read the source"))
        return blocks(tool_call("read_file", path="source.txt"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent", task_id=task.id, require_plan=True,
                     tools=["submit_plan", "read_file"], max_rounds=1, max_tool_calls=1)
    events = []
    wait_event(runtime, "idle_notification", received=events)
    agent_id = creation["agent_id"]
    original = subagent_runtime.get_subagent_output(agent_id, block=False)
    plan = next(message for message in events if message["type"] == "plan_approval_request")
    assert subagent_runtime.run_review_plan(plan["metadata"]["request_id"], True).status == "success"
    assert waiting_again.wait(2)

    assert len(calls) == 1
    assert subagent_runtime.get_subagent_output(agent_id, block=False) == original
    assert subagent_runtime.list_subagents()[0]["status"] == "idle"
    assert task_state.assignments["worker"]["task_id"] == task.id
    assert not subagent_runtime.consume_lead_inbox()

    subagent_runtime.send_message("lead", agent_id, "Continue reading the source")
    wait_event(runtime, "idle_notification")
    result = subagent_runtime.get_subagent_output(agent_id, block=False)
    assert result["status"] == "budget_exhausted"
    assert "Current evidence" in result["text"]
    assert result["rounds"] == 1 and result["tool_calls"] == 1
    assert result["usage"]["model_calls"] == 2
    assert len(calls) == 2


def test_new_plan_request_updates_assignment_version_before_approval(runtime, monkeypatch):
    calls = []
    delivered = threading.Event()
    release = threading.Event()
    notify = subagent_runtime._notify_result

    def hold_result(record, result):
        notify(record, result)
        if result["status"] == "completed" and not delivered.is_set():
            delivered.set()
            assert release.wait(3)

    monkeypatch.setattr(subagent_runtime, "_notify_result", hold_result)

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return blocks(text_block("First finding"))
        if len(calls) == 2:
            return blocks(tool_call("write_file", path="premature.txt", content="changed"),
                          tool_call("submit_plan", plan="Write after approval"))
        if len(calls) == 3:
            return blocks(tool_call("write_file", path="approved.txt", content="changed"))
        return blocks(text_block("Second finding"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent", tools=["write_file", "submit_plan"])
    try:
        assert delivered.wait(2)
        assert subagent_runtime.run_request_plan(creation["agent_id"], "Plan the next work").status == "success"
    finally:
        release.set()
    plan = wait_event(runtime, "plan_approval_request")
    assert not core.WORKDIR.joinpath("premature.txt").exists()
    assert subagent_runtime.run_review_plan(plan["metadata"]["request_id"], True).status == "success"
    finished = subagent_runtime.get_subagent_output(creation["agent_id"], timeout=2)
    assert finished["text"] == "Second finding"
    assert core.WORKDIR.joinpath("approved.txt").read_text() == "changed"
    assert "Blocked: plan status is required" in str(calls[2]["messages"])
    assert len(calls) == 4


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


def test_queued_approval_and_new_work_use_the_new_delegation(runtime, monkeypatch):
    calls = []
    received = threading.Event()
    release = threading.Event()
    wait = subagent_runtime._BUS.wait_for_messages

    def hold_approval(name, timeout=None):
        messages = wait(name, timeout)
        if name == "worker" and any(message["type"] == "plan_approval_response" for message in messages):
            received.set()
            assert release.wait(3)
        return messages

    monkeypatch.setattr(subagent_runtime._BUS, "wait_for_messages", hold_approval)
    task = task_state.create_task("Inspect source")

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return blocks(tool_call("submit_plan", plan="Read the source"))
        return blocks(text_block("New finding"))

    client(runtime, create)
    creation = spawn(runtime, mode="persistent", task_id=task.id, require_plan=True,
                     tools=["submit_plan"], max_tool_calls=1)
    events = []
    wait_event(runtime, "idle_notification", received=events)
    plan = next(message for message in events if message["type"] == "plan_approval_request")
    try:
        assert subagent_runtime.run_review_plan(plan["metadata"]["request_id"], True).status == "success"
        assert received.wait(2)
        subagent_runtime.send_message("lead", creation["agent_id"], "Investigate B")
    finally:
        release.set()
    finished = subagent_runtime.get_subagent_output(creation["agent_id"], timeout=2)
    assert "New finding" in finished["text"]
    context = next(message["content"] for message in reversed(calls[1]["messages"])
                   if isinstance(message["content"], str) and "Current delegation:" in message["content"])
    assert "Current delegation: Investigate B" in context
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
