"""TurnEngine 的接口级行为。"""

import copy
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_harness import host
from agent_harness.agents import recovery, turn_engine
from agent_harness.agents.turn_engine import (
    PreparedRequest, TurnCallbacks, TurnEngine, TurnPhase, TurnRequest,
)
from agent_harness.providers.llm_client import model_messages
from agent_harness.runtime.interaction import (
    ExecutionContext, InteractionChannel, execution_context,
)
from agent_harness.runtime import interaction


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


def _callbacks(call_llm, *, assemble_tool_pool=None, evaluate_goal=None, goal_condition=None):
    return TurnCallbacks(
        assemble_tool_pool=assemble_tool_pool or (lambda: ([], {})),
        build_context=lambda history: SimpleNamespace(prompt_values=lambda: {}),
        call_llm=call_llm,
        build_user_content=lambda results: results,
        evaluate_goal=evaluate_goal or (lambda history: None),
        remember_after_turn=lambda history: None,
        goal_condition=goal_condition or (lambda: ""),
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


def test_tool_results_are_paired_and_prepare_a_new_request(monkeypatch):
    prompt_state = _PromptState()
    block = SimpleNamespace(type="tool_use", id="read-1", name="read_file", input={"path": "a.txt"})
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    calls = []
    monkeypatch.setattr(turn_engine, "execute_tool_batch", lambda *args: [{
        "type": "tool_result", "tool_use_id": "read-1", "content": "file contents"
    }])

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

    monkeypatch.setattr(turn_engine.compaction, "reactive_compact", compact)

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
def test_tool_round_trip_records_tool_execution_phase(monkeypatch):
    block = SimpleNamespace(type="tool_use", id="call-1", name="read_file", input={})
    responses = iter([
        SimpleNamespace(stop_reason="tool_use", content=[block], usage=None),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")], usage=None),
    ])
    monkeypatch.setattr(turn_engine, "execute_tool_batch", lambda *args: [])
    engine = TurnEngine(TurnCallbacks(
        assemble_tool_pool=lambda: ([], {}),
        build_context=lambda history: SimpleNamespace(prompt_values=lambda: {}),
        call_llm=lambda *args: (next(responses), None, None),
        build_user_content=lambda results: results,
        evaluate_goal=lambda history: None,
        remember_after_turn=lambda history: None,
        goal_condition=lambda: "",
    ))

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


def test_tool_approval_is_recorded_in_turn_phase_history(monkeypatch):
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
        return []

    monkeypatch.setattr(turn_engine, "execute_tool_batch", execute)
    engine = TurnEngine(TurnCallbacks(
        assemble_tool_pool=lambda: ([], {}),
        build_context=lambda history: SimpleNamespace(prompt_values=lambda: {}),
        call_llm=lambda *args: (next(responses), None, None),
        build_user_content=lambda results: results,
        evaluate_goal=lambda history: None,
        remember_after_turn=lambda history: None,
        goal_condition=lambda: "",
    ))
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
