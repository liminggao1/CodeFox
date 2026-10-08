"""Lead recovery trajectories use one owner for output and overload state."""

from types import SimpleNamespace

import pytest

from agent_harness import recovery


def test_429_breaks_consecutive_529_sequence(monkeypatch):
    monkeypatch.setattr(recovery.core, "FALLBACK_MODEL", "fallback")
    monkeypatch.setattr(recovery.core, "MAX_RETRIES", 4)
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    state = recovery.RecoveryState()
    trace = []
    outcomes = [RuntimeError("529 overloaded"), RuntimeError("429 limit"), RuntimeError("529 overloaded"), "ok"]

    def call():
        trace.append((state.current_model, state.consecutive_529))
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    assert recovery.with_retry(call, state) == "ok"
    assert [count for _, count in trace] == [0, 1, 0, 1]
    assert all(model == recovery.core.PRIMARY_MODEL for model, _ in trace)
    assert state.consecutive_529 == 0


def test_consecutive_529_switches_model_and_success_keeps_output_upgrade(monkeypatch):
    monkeypatch.setattr(recovery.core, "FALLBACK_MODEL", "fallback")
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    state = recovery.RecoveryState()
    assert state.accept_response("max_tokens") == "retry"
    models = []

    def call():
        models.append(state.current_model)
        if len(models) < 3:
            raise RuntimeError("529 overloaded")
        return "ok"

    assert recovery.with_retry(call, state) == "ok"
    assert models == [recovery.core.PRIMARY_MODEL, recovery.core.PRIMARY_MODEL, "fallback"]
    assert state.max_tokens == recovery.core.ESCALATED_MAX_TOKENS
    assert state.accept_response("max_tokens") == "continue"


def test_retry_exhaustion_still_has_a_fixed_limit(monkeypatch):
    monkeypatch.setattr(recovery.time, "sleep", lambda _: None)
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("429 limit")

    with pytest.raises(RuntimeError, match="Max retries"):
        recovery.with_retry(fail, recovery.RecoveryState())
    assert len(calls) == recovery.core.MAX_RETRIES


def test_output_transitions_preserve_turn_total_and_reset_only_on_new_turn():
    state = recovery.RecoveryState()
    assert state.accept_response("max_tokens") == "retry"
    assert state.max_tokens == recovery.core.ESCALATED_MAX_TOKENS
    assert state.accept_response("max_tokens") == "continue"
    assert state.accept_response("tool_use") == "accept"
    assert state.max_tokens == recovery.core.DEFAULT_MAX_TOKENS
    assert state.accept_response("max_tokens") == "retry"
    for _ in range(recovery.core.MAX_RECOVERY_RETRIES - 1):
        assert state.accept_response("max_tokens") == "continue"
    assert state.accept_response("max_tokens") == "stop"
    fresh = recovery.RecoveryState()
    assert fresh.recovery_count == 0
    assert fresh.current_model == recovery.core.PRIMARY_MODEL
    assert fresh.max_tokens == recovery.core.DEFAULT_MAX_TOKENS


def test_reactive_opportunity_is_consumed_once_and_fields_are_readonly():
    state = recovery.RecoveryState()
    assert state.claim_reactive_compaction()
    assert not state.claim_reactive_compaction()
    assert recovery.RecoveryState().claim_reactive_compaction()
    with pytest.raises(AttributeError):
        state.max_tokens = 1
    with pytest.raises(AttributeError):
        state.current_model = "external"


def test_lead_uses_state_limits_and_preserves_only_continuable_text(host_factory, monkeypatch):
    host = host_factory()
    calls = []
    responses = [SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])
                 for stop, text in [("max_tokens", "discarded"), ("max_tokens", "partial"),
                                    ("end_turn", "done"), ("end_turn", "next")]]

    def create(**kwargs):
        calls.append(kwargs["max_tokens"])
        return responses.pop(0)

    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    history = [{"role": "user", "content": "go"}]
    host.agent_loop(history, "go")
    assert "discarded" not in str(history)
    assert "partial" in str(history)
    assert host.core.CONTINUATION_PROMPT in str(history)
    host.agent_loop(history, "next")
    assert calls == [host.core.DEFAULT_MAX_TOKENS, host.core.ESCALATED_MAX_TOKENS,
                     host.core.ESCALATED_MAX_TOKENS, host.core.DEFAULT_MAX_TOKENS]


def test_lead_does_not_repeat_reactive_recovery(host_factory, monkeypatch):
    host = host_factory()
    calls = []
    summaries = []

    def create(**kwargs):
        calls.append(True)
        raise RuntimeError("context_length_exceeded")

    def summary(messages):
        summaries.append(True)
        return "summary"

    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(host.compaction, "summarize_history", summary)
    history = [{"role": "user", "content": "go"}]
    host.agent_loop(history, "go")
    assert calls == [True, True]
    assert summaries == [True]
    assert "context_length_exceeded" in history[-1]["content"][0]["text"]
