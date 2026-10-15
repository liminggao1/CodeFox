"""TurnEngine 的接口级行为。"""

import copy
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_harness import host
from agent_harness.agents import recovery
from agent_harness.agents.lead_turn import LeadTurn
from agent_harness.agents.subagent_session import SubagentSession
from agent_harness.agents.turn_engine import (
    PreparedRequest, TurnCallbacks, TurnEngine, TurnPhase, TurnRequest,
)
from agent_harness.providers.llm_client import model_messages
from agent_harness.runtime.interaction import (
    ExecutionContext, InteractionChannel, execution_context,
)
from agent_harness.runtime import interaction
from agent_harness.runtime.tool_execution import ToolExecution
from agent_harness.tools.tool_result import ToolResult
from agent_harness.context import compaction
from agent_harness.context.prompt_state import is_context_message


class _PromptState:
    def __init__(self):
        self.prepared = []
        self._snapshot = None

    def snapshot(self):
        return copy.deepcopy(self._snapshot)

    def reset(self):
        self._snapshot = None

    def prepare(self, messages, values, system_factory, active_request, **kwargs):
        system = "system"
        self.prepared.append((system, copy.deepcopy(model_messages(messages)), copy.deepcopy(kwargs["tools"])))
        self._snapshot = {"prepared": len(self.prepared)}
        return system, SimpleNamespace(applied=False, reason=None)


def _response(text="done", *, blocks=(), stop="end_turn"):
    return SimpleNamespace(stop_reason=stop,
                           content=[SimpleNamespace(type="text", text=text), *blocks], usage=None)


def _callbacks(call_llm, *, assemble_tool_pool=None, evaluate_goal=None, goal_condition=None,
               execute_tool=None, before_iteration=None):
    turn = LeadTurn([], evaluate_goal=evaluate_goal, goal_condition=goal_condition)
    return TurnCallbacks(
        turn=turn,
        assemble_tool_pool=assemble_tool_pool or (lambda: ([], {})),
        build_context=lambda history, request: ({}, request),
        system_factory=lambda: "system",
        call_llm=lambda prepared: call_llm(prepared, turn.recovery),
        execute_tool=execute_tool or (lambda *args: ToolExecution(ToolResult("file contents"))),
        before_iteration=before_iteration,
    )


def test_prepared_request_matches_counted_payload_and_strips_internal_marker():
    prompt_state = _PromptState()
    history = [{"role": "user", "content": "hello", "_runtime_context": True}]
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    observed = []
    engine = TurnEngine(_callbacks(
        lambda prepared, state: (observed.append(prepared) or (_response(), None, None)),
        assemble_tool_pool=lambda: (tools, {}),
    ))

    outcome = engine.run(TurnRequest(history, "hello", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert engine.phase is TurnPhase.COMPLETED
    assert len(observed) == 1
    assert isinstance(observed[0], PreparedRequest)
    assert (observed[0].system, observed[0].messages, observed[0].tools) == prompt_state.prepared[0]
    assert "_runtime_context" not in observed[0].messages[0]
    assert engine.phase_history == (
        TurnPhase.CREATED,
        TurnPhase.PREPARING,
        TurnPhase.WAITING_MODEL,
        TurnPhase.PROCESSING_RESPONSE,
        TurnPhase.COMPLETED,
    )
    assert history[-1]["role"] == "assistant"


def test_max_tokens_retry_reuses_detached_request_snapshot():
    prompt_state = _PromptState()
    history = [{"role": "user", "content": "original"}]
    tools = [{"name": "read_file"}]
    calls, token_limits = [], []

    def call_llm(prepared, state):
        calls.append(prepared)
        token_limits.append(state.max_tokens)
        if len(calls) == 1:
            history.append({"role": "user", "content": "mutated during retry"})
            return _response("partial", stop="max_tokens"), None, None
        return _response("complete"), None, None

    engine = TurnEngine(_callbacks(call_llm, assemble_tool_pool=lambda: (tools, {})))
    outcome = engine.run(TurnRequest(history, "original", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert len(calls) == 2 and calls[0] is calls[1]
    assert calls[1].messages == [{"role": "user", "content": "original"}]
    assert len(prompt_state.prepared) == 1
    assert token_limits[1] > token_limits[0]


def test_request_usage_callback_records_each_successful_response_once():
    responses = iter([_response("partial", stop="max_tokens"), _response("done")])
    observed = []
    channel = InteractionChannel()
    context = ExecutionContext(channel, "session", 1, "turn")

    def call_llm(*args):
        response = next(responses)
        response.usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        return response, None, None

    engine = TurnEngine(_callbacks(call_llm))
    with execution_context(context):
        outcome = engine.run(TurnRequest(
            [], "request", on_usage=observed.append, prompt_state=_PromptState()))

    assert outcome.status == "completed"
    assert observed == [{"input_tokens": 10, "output_tokens": 5}] * 2
    events = []
    while (event := channel.next_event()) is not None:
        events.append(event)
    assert [event.payload["usage"] for event in events if event.type == "lead_usage"] == observed


def test_tool_results_are_paired_and_prepare_a_new_request():
    prompt_state = _PromptState()
    block = SimpleNamespace(type="tool_use", id="read-1", name="read_file", input={"path": "a.txt"})
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    calls = []

    def call_llm(prepared, state):
        calls.append(prepared)
        response = _response("", blocks=[block], stop="tool_use") if len(calls) == 1 else _response("finished")
        return response, None, None

    engine = TurnEngine(_callbacks(
        call_llm, assemble_tool_pool=lambda: (tools, {"read_file": object()}),
    ))
    outcome = engine.run(TurnRequest([{"role": "user", "content": "read"}], "read", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert len(calls) == 2 and calls[0] is not calls[1]
    assert calls[1].messages[-2]["content"][1].id == "read-1"
    assert calls[1].messages[-1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "read-1", "content": "file contents"
    }


def test_goal_block_adds_feedback_before_next_prepared_request():
    prompt_state = _PromptState()
    decisions = iter([SimpleNamespace(action="block", reason="need evidence"), None])
    calls = []

    def call_llm(prepared, state):
        calls.append(prepared)
        return _response("working" if len(calls) == 1 else "done"), None, None

    engine = TurnEngine(_callbacks(
        call_llm, evaluate_goal=lambda history: next(decisions),
        goal_condition=lambda: "show evidence",
    ))
    outcome = engine.run(TurnRequest([{"role": "user", "content": "task"}], "task", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert len(calls) == 2
    assert "[Goal still active]" in calls[1].messages[-1]["content"]


def test_reactive_compaction_prepares_a_fresh_request(monkeypatch):
    prompt_state = _PromptState()
    calls = []

    def compact(history, active_request):
        history[:] = [{"role": "user", "content": "compressed"}]
        return SimpleNamespace(applied=True, reason=None)

    monkeypatch.setattr(compaction, "reactive_compact", compact)

    def call_llm(prepared, state):
        calls.append(prepared)
        if len(calls) == 1:
            raise RuntimeError("prompt is too long")
        return _response("done"), None, None

    engine = TurnEngine(_callbacks(call_llm))
    outcome = engine.run(TurnRequest([{"role": "user", "content": "long"}], "long", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert len(calls) == 2 and calls[0] is not calls[1]
    assert calls[1].messages[0]["content"] == "compressed"
    assert len(prompt_state.prepared) == 2


def test_model_failure_sets_failed_terminal_phase():
    engine = TurnEngine(_callbacks(
        lambda prepared, state: (_ for _ in ()).throw(RuntimeError("model failed"))
    ))
    outcome = engine.run(TurnRequest([], "request", prompt_state=_PromptState()))

    assert outcome.status == "failed"
    assert "model failed" in outcome.reason
    assert engine.phase is TurnPhase.FAILED


def test_tool_round_trip_records_tool_execution_phase():
    block = SimpleNamespace(type="tool_use", id="call-1", name="read_file", input={})
    responses = iter([
        SimpleNamespace(stop_reason="tool_use", content=[block], usage=None),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")], usage=None),
    ])
    engine = TurnEngine(_callbacks(lambda *args: (next(responses), None, None)))

    outcome = engine.run(TurnRequest([], "read", prompt_state=_PromptState()))

    assert outcome.status == "completed"
    assert engine.phase_history == (
        TurnPhase.CREATED,
        TurnPhase.PREPARING,
        TurnPhase.WAITING_MODEL,
        TurnPhase.PROCESSING_RESPONSE,
        TurnPhase.EXECUTING_TOOLS,
        TurnPhase.PREPARING,
        TurnPhase.WAITING_MODEL,
        TurnPhase.PROCESSING_RESPONSE,
        TurnPhase.COMPLETED,
    )


def test_tool_approval_is_recorded_in_turn_phase_history():
    channel = InteractionChannel()
    block = SimpleNamespace(type="tool_use", id="call-1", name="bash", input={})
    responses = iter([
        SimpleNamespace(stop_reason="tool_use", content=[block], usage=None),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")], usage=None),
    ])

    def execute(*args):
        context = interaction.current_context()

        def approve():
            event = channel.next_event(timeout=2)
            if event is not None:
                assert channel.acknowledge_event(event.sequence)
            request = channel.next_approval(timeout=2)
            assert request is not None
            assert channel.respond(request.request_id, request.generation, "approve")

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(approve)
            assert context.channel.request_approval(context, "bash", block.id, block.input) == "approve"
            waiting.result(timeout=2)
        return ToolExecution(ToolResult("approved"))

    engine = TurnEngine(_callbacks(lambda *args: (next(responses), None, None), execute_tool=execute))
    context = ExecutionContext(channel, "session", 1, "turn", origin="frontend")

    with execution_context(context):
        outcome = engine.run(TurnRequest([], "run", prompt_state=_PromptState()))

    assert outcome.status == "completed"
    assert engine.phase_history.count(TurnPhase.WAITING_APPROVAL) == 1
    assert engine.phase_history[4:7] == (
        TurnPhase.EXECUTING_TOOLS,
        TurnPhase.WAITING_APPROVAL,
        TurnPhase.EXECUTING_TOOLS,
    )


def test_phase_history_ignores_duplicates_and_rejects_invalid_transitions():
    engine = TurnEngine(_callbacks(lambda *args: None))

    engine._set_phase(TurnPhase.CREATED)
    assert engine.phase_history == (TurnPhase.CREATED,)
    with pytest.raises(RuntimeError, match="非法 TurnPhase 转换"):
        engine._set_phase(TurnPhase.WAITING_MODEL)


def test_run_stops_before_model_when_context_is_cancelled():
    channel = InteractionChannel()
    callbacks = _callbacks(
        lambda *args: (_ for _ in ()).throw(AssertionError("not reached")),
        assemble_tool_pool=lambda: (_ for _ in ()).throw(AssertionError("not reached")),
    )
    engine = TurnEngine(callbacks)
    stop = threading.Event()
    stop.set()
    context = ExecutionContext(channel, "session", 1, "turn", stop_event=stop)
    with execution_context(context):
        outcome = engine.run(TurnRequest([], "cancelled", prompt_state=_PromptState()))

    assert outcome.status == "stopped"
    assert engine.phase is TurnPhase.STOPPED


def test_host_sends_prepared_request_without_rebuilding_payload(monkeypatch):
    captured = {}
    expected = PreparedRequest("fixed system", [{"role": "user", "content": "request"}], [{"name": "read_file"}])
    fake_response = SimpleNamespace(stop_reason="end_turn", content=[])

    def create(**kwargs):
        captured.update(kwargs)
        return fake_response

    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    result, call_id, operation_id = host.call_llm(expected, recovery.RecoveryState())

    assert result is fake_response
    assert call_id is operation_id is None
    assert captured["system"] is expected.system
    assert captured["messages"] is expected.messages
    assert captured["tools"] is expected.tools


@pytest.mark.parametrize("owner", ["lead", "once", "persistent"])
def test_shared_loop_returns_full_work_and_pairs_denials(host_factory, owner):
    host_factory()
    block = SimpleNamespace(type="tool_use", id="read", name="read_file", input={"path": "file.txt"})
    values = iter([_response("", blocks=[block]), _response("finished")])
    calls, executed = [], []

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return next(values)

    def execute(*args):
        executed.append(args[0].id)
        return ToolExecution(ToolResult("Denied for this work", "denied"))

    history = [{"role": "user", "content": "inspect"}]
    channel = InteractionChannel()
    context = ExecutionContext(channel, "session", 1, "turn", source=owner)
    with execution_context(context):
        if owner == "lead":
            engine = TurnEngine(_callbacks(
                lambda prepared, state: (create(messages=prepared.messages), "lead-call", None),
                execute_tool=execute))
            assert engine.run(TurnRequest(history, "inspect", prompt_state=_PromptState())).status == "completed"
        else:
            child = SubagentSession(owner, SimpleNamespace(messages=SimpleNamespace(create=create)),
                                    "child-model", "Child rules", mode=owner, tools=["read_file"])
            child.execute_tool = execute
            final = child.run(history)
            assert final.content[0].text == "finished"
            assert child.rounds == 2
            engine = child.engine

    assert len(calls) == 2 and executed == ["read"]
    assert calls[1]["messages"][-1]["content"] == [{
        "type": "tool_result", "tool_use_id": "read", "content": "Denied for this work", "is_error": True}]
    business = [item for item in history if not is_context_message(item)]
    assert [item["role"] for item in business] == ["user", "assistant", "user", "assistant"]
    assert engine.phase is TurnPhase.COMPLETED
    events = []
    while (event := channel.next_event()) is not None:
        events.append(event)
    assert len([event for event in events if event.type == "tool_call"]) == 1
    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1 and results[0].payload["status"] == "denied"


@pytest.mark.parametrize("owner", ["lead", "once", "persistent"])
@pytest.mark.parametrize("hook", ["before", "after"])
def test_shared_batch_preserves_results_before_propagating_hook_errors(host_factory, owner, hook):
    host_factory()
    blocks = [SimpleNamespace(type="tool_use", id=call_id, name="read_file", input={})
              for call_id in ("first", "second")]
    executed = []
    error = RuntimeError("hook failed")

    def execute(block, *args):
        executed.append(block.id)
        if hook == "before":
            raise error
        return ToolExecution(ToolResult("Real result"), error)

    history = [{"role": "user", "content": "inspect"}]
    channel = InteractionChannel()
    with execution_context(ExecutionContext(channel, "session", 1, "turn", source=owner)):
        if owner == "lead":
            engine = TurnEngine(_callbacks(
                lambda *args: (_response("", blocks=blocks), "lead-call", None), execute_tool=execute))
            run = lambda: engine.run(TurnRequest(history, "inspect", prompt_state=_PromptState()))
        else:
            child = SubagentSession(owner, SimpleNamespace(messages=SimpleNamespace(
                create=lambda **kwargs: _response("", blocks=blocks))), "child-model", "Child rules",
                mode=owner, tools=["read_file"])
            child.execute_tool = execute
            run = lambda: child.run(history)
        with pytest.raises(RuntimeError, match="hook failed"):
            run()
        if owner != "lead":
            engine = child.engine

    assert executed == ["first"] and engine.phase is TurnPhase.FAILED
    results = history[-1]["content"]
    assert [item["tool_use_id"] for item in results] == ["first", "second"]
    assert bool(results[0].get("is_error")) is (hook == "before")
    assert results[1]["is_error"] and "Interrupted" in results[1]["content"]
    events = []
    while (event := channel.next_event()) is not None:
        events.append(event)
    result_events = [event for event in events if event.type == "tool_result"]
    assert [event.payload["tool_use_id"] for event in result_events] == ["first", "second"]
    assert result_events[1].payload["status"] == "stopped"


def test_nested_child_approval_restores_parent_phase_observer(host_factory):
    host_factory()
    channel = InteractionChannel()
    outer_calls = iter([_response("", blocks=[SimpleNamespace(
        type="tool_use", id="spawn", name="spawn_subagent", input={})]), _response()])
    child_calls = iter([_response("", blocks=[SimpleNamespace(
        type="tool_use", id="read", name="read_file", input={})]), _response()])
    child = SubagentSession("child", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: next(child_calls))), "child-model", "Child rules",
        mode="once", tools=["read_file"])

    def execute_child(block):
        context = interaction.current_context()

        def approve():
            # 事件交付屏障必须先确认，再取审批请求。
            while (event := channel.next_event(timeout=0.01)) is not None:
                channel.acknowledge_event(event.sequence)
            request = channel.next_approval(timeout=2)
            assert request is not None
            assert channel.respond(request.request_id, request.generation, "approve")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(approve)
            assert context.channel.request_approval(context, block.name, block.id, block.input) == "approve"
            future.result(timeout=2)
        return ToolExecution(ToolResult("approved"))

    child.execute_tool = execute_child

    def execute_outer(*args):
        parent = interaction.current_context()
        observer = parent.phase_observer
        with execution_context(parent.delegate("child")):
            child.run([{"role": "user", "content": "inspect"}])
            assert interaction.current_context().phase_observer is observer
        assert engine.phase is TurnPhase.EXECUTING_TOOLS
        return ToolExecution(ToolResult("child finished"))

    engine = TurnEngine(_callbacks(lambda *args: (next(outer_calls), "parent-call", None),
                                   execute_tool=execute_outer))
    parent_context = ExecutionContext(channel, "session", 1, "turn", origin="frontend")
    with execution_context(parent_context):
        assert engine.run(TurnRequest([], "delegate", prompt_state=_PromptState())).status == "completed"
        assert interaction.current_context() is parent_context
    assert TurnPhase.WAITING_APPROVAL not in engine.phase_history
    assert child.engine.phase_history[4:7] == (
        TurnPhase.EXECUTING_TOOLS, TurnPhase.WAITING_APPROVAL, TurnPhase.EXECUTING_TOOLS)


def test_iteration_plan_wait_resumes_preparation_without_extra_model_call(host_factory):
    host_factory()
    calls = []
    child = SubagentSession("child", SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: calls.append(kwargs) or _response())), "child-model", "Child rules",
        mode="persistent", tools=[])

    def coordinate():
        with child.engine.waiting_approval():
            assert child.engine.phase is TurnPhase.WAITING_APPROVAL
            assert child.rounds == child.tool_calls == 0
        assert child.engine.phase is TurnPhase.PREPARING

    child.run([{"role": "user", "content": "inspect"}], before_iteration=coordinate)
    assert len(calls) == 1 and child.rounds == 1
    assert child.engine.phase_history == (
        TurnPhase.CREATED, TurnPhase.PREPARING, TurnPhase.WAITING_APPROVAL,
        TurnPhase.PREPARING, TurnPhase.WAITING_MODEL, TurnPhase.PROCESSING_RESPONSE, TurnPhase.COMPLETED)


def test_result_notification_failure_keeps_paired_history_before_raising():
    from dataclasses import replace
    block = SimpleNamespace(type="tool_use", id="read", name="read_file", input={})
    callbacks = _callbacks(lambda *args: (_response("", blocks=[block]), None, None))
    callbacks = replace(callbacks, build_user_content=lambda results: (
        _ for _ in ()).throw(OSError("notification failed")))
    engine = TurnEngine(callbacks)
    history = [{"role": "user", "content": "Inspect"}]
    with pytest.raises(OSError, match="notification failed"):
        engine.run(TurnRequest(history, "Inspect", prompt_state=_PromptState()))
    assert history[-1]["content"] == [{
        "type": "tool_result", "tool_use_id": "read", "content": "file contents"}]
    assert engine.phase is TurnPhase.FAILED
