"""TurnEngine 的接口级行为。"""

import copy
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from codefox import core
from codefox.agents.lead_turn import LeadTurn
from codefox.agents.subagent_session import SubagentSession
from codefox.agents.turn_engine import (
    PreparedRequest, TurnCallbacks, TurnEngine, TurnPhase, TurnRequest,
)
from codefox.providers.llm_client import model_messages
from codefox.runtime.interaction import (
    ExecutionContext, InteractionChannel, execution_context,
)
from codefox.runtime import interaction
from codefox.runtime.tool_execution import ToolExecution
from codefox.tools.tool_result import ToolResult
from codefox.context import compaction
from support.fakes import response


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
        lambda prepared, state: (observed.append(prepared) or (response("done", usage=None), None, None)),
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
            return response("partial", stop="max_tokens", usage=None), None, None
        return response("complete", usage=None), None, None

    engine = TurnEngine(_callbacks(call_llm, assemble_tool_pool=lambda: (tools, {})))
    outcome = engine.run(TurnRequest(history, "original", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert len(calls) == 2 and calls[0] is calls[1]
    assert calls[1].messages == [{"role": "user", "content": "original"}]
    assert len(prompt_state.prepared) == 1
    assert token_limits[1] > token_limits[0]


def test_output_continuation_preserves_text_and_stops_at_limit(monkeypatch):
    monkeypatch.setattr(core, "MAX_RECOVERY_RETRIES", 1)
    for exhausted in (False, True):
        responses = iter([
            response("discarded", stop="max_tokens", usage=None),
            response("first part", stop="max_tokens", usage=None),
            response("last part", stop="max_tokens" if exhausted else "end_turn", usage=None),
        ])
        calls = []

        def call_llm(prepared, state):
            calls.append(prepared)
            return next(responses), None, None

        history = [{"role": "user", "content": "write"}]
        engine = TurnEngine(_callbacks(call_llm))
        outcome = engine.run(TurnRequest(history, "write", prompt_state=_PromptState()))

        assert outcome.status == ("truncated" if exhausted else "completed")
        assert len(calls) == 3 and calls[0] is calls[1]
        assert calls[2] is not calls[1]
        assert calls[2].messages[-1] == {"role": "user", "content": core.CONTINUATION_PROMPT}
        assert [message["content"][0].text for message in history
                if message["role"] == "assistant"] == ["first part", "last part"]
        if exhausted:
            assert "continuation limit" in outcome.reason


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
        return response("done", usage=None), None, None

    engine = TurnEngine(_callbacks(call_llm))
    outcome = engine.run(TurnRequest([{"role": "user", "content": "long"}], "long", prompt_state=prompt_state))

    assert outcome.status == "completed"
    assert len(calls) == 2 and calls[0] is not calls[1]
    assert calls[1].messages[0]["content"] == "compressed"
    assert len(prompt_state.prepared) == 2


def test_phase_history_ignores_duplicates_and_rejects_invalid_transitions():
    engine = TurnEngine(_callbacks(lambda *args: None))

    engine._set_phase(TurnPhase.CREATED)
    assert engine.phase_history == (TurnPhase.CREATED,)
    with pytest.raises(RuntimeError, match="非法 TurnPhase 转换"):
        engine._set_phase(TurnPhase.WAITING_MODEL)


def test_run_stops_before_model_when_context_is_cancelled():
    channel = InteractionChannel()
    for cancel_after_preparation in (False, True):
        stop = threading.Event()
        if not cancel_after_preparation:
            stop.set()
        callbacks = _callbacks(
            lambda *args: (_ for _ in ()).throw(AssertionError("not reached")),
            before_iteration=stop.set if cancel_after_preparation else None,
        )
        engine = TurnEngine(callbacks)
        context = ExecutionContext(channel, "session", 1, "turn", stop_event=stop)
        with execution_context(context):
            outcome = engine.run(TurnRequest([], "cancelled", prompt_state=_PromptState()))

        assert outcome.status == "stopped"
        assert engine.phase is TurnPhase.STOPPED


@pytest.mark.parametrize("owner,hook", [("lead", "before"), ("once", "after"), ("persistent", "before")])
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
                lambda *args: (response("", tools=blocks, usage=None), "lead-call", None), execute_tool=execute))
            run = lambda: engine.run(TurnRequest(history, "inspect", prompt_state=_PromptState()))
        else:
            child = SubagentSession(owner, SimpleNamespace(messages=SimpleNamespace(
                create=lambda **kwargs: response("", tools=blocks, usage=None))), "child-model", "Child rules",
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
    outer_calls = iter([response("", tools=[SimpleNamespace(
        type="tool_use", id="spawn", name="spawn_subagent", input={})], usage=None), response("done", usage=None)])
    child_calls = iter([response("", tools=[SimpleNamespace(
        type="tool_use", id="read", name="read_file", input={})], usage=None), response("done", usage=None)])
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


def test_result_notification_failure_keeps_paired_history_before_raising():
    from dataclasses import replace
    block = SimpleNamespace(type="tool_use", id="read", name="read_file", input={})
    callbacks = _callbacks(lambda *args: (response("", tools=[block], usage=None), None, None))
    callbacks = replace(callbacks, build_user_content=lambda results: (
        _ for _ in ()).throw(OSError("notification failed")))
    engine = TurnEngine(callbacks)
    history = [{"role": "user", "content": "Inspect"}]
    with pytest.raises(OSError, match="notification failed"):
        engine.run(TurnRequest(history, "Inspect", prompt_state=_PromptState()))
    assert history[-1]["content"] == [{
        "type": "tool_result", "tool_use_id": "read", "content": "file contents"}]
    assert engine.phase is TurnPhase.FAILED
