"""TurnEngine 的接口级行为。"""

from types import SimpleNamespace

from agent_harness.agents.turn_engine import TurnCallbacks, TurnEngine, TurnPhase, TurnRequest
from agent_harness.runtime.interaction import ExecutionContext, InteractionChannel, execution_context


class _PromptState:
    def snapshot(self):
        return None

    def prepare(self, *args, **kwargs):
        return "system", SimpleNamespace(applied=False, reason=None)


def _engine(response):
    callbacks = TurnCallbacks(
        assemble_tool_pool=lambda: ([], {}),
        build_context=lambda history: SimpleNamespace(prompt_values=lambda: {}),
        call_llm=lambda *args: (response, None, None),
        build_user_content=lambda results: results,
        evaluate_goal=lambda history: None,
        remember_after_turn=lambda history: None,
        goal_condition=lambda: "",
    )
    return TurnEngine(callbacks)


def test_run_exposes_completed_phase_for_plain_response():
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="done")],
        usage=None,
    )
    engine = _engine(response)
    history = [{"role": "user", "content": "hello"}]

    outcome = engine.run(TurnRequest(history, "hello", prompt_state=_PromptState()))

    assert outcome.status == "completed"
    assert engine.phase is TurnPhase.COMPLETED
    assert history[-1]["role"] == "assistant"


def test_run_stops_before_model_when_context_is_cancelled():
    channel = InteractionChannel()
    callbacks = TurnCallbacks(
        assemble_tool_pool=lambda: (_ for _ in ()).throw(AssertionError("not reached")),
        build_context=lambda history: SimpleNamespace(prompt_values=lambda: {}),
        call_llm=lambda *args: (_ for _ in ()).throw(AssertionError("not reached")),
        build_user_content=lambda results: results,
        evaluate_goal=lambda history: None,
        remember_after_turn=lambda history: None,
        goal_condition=lambda: "",
    )
    engine = TurnEngine(callbacks)

    # Use the shared context contract with a pre-set stop event.
    import threading
    stop = threading.Event()
    stop.set()
    context = ExecutionContext(channel, "session", 1, "turn", stop_event=stop)
    with execution_context(context):
        outcome = engine.run(TurnRequest([], "cancelled", prompt_state=_PromptState()))

    assert outcome.status == "stopped"
    assert engine.phase is TurnPhase.STOPPED
