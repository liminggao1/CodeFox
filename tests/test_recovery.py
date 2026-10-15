"""Lead recovery trajectories use one owner for output and overload state."""

import codefox.core as core
import codefox.context.compaction as compaction
import codefox.context.memory_runtime as memory_runtime
import codefox.tools.mcp_tools as mcp_tools
import codefox.runtime.scheduler as scheduler


from types import SimpleNamespace

import pytest

from codefox.agents import recovery


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


@pytest.mark.parametrize("failure", ["memory", "tools", "preparation_context_error"])
def test_preparation_failure_restores_cron_and_next_turn_can_deliver(host_factory, monkeypatch, failure):
    host = host_factory()
    calls = []

    def create(**kwargs):
        calls.append(True)
        return SimpleNamespace(stop_reason="end_turn", content=[])

    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    original_index = memory_runtime.read_memory_index
    if failure == "tools":
        client = mcp_tools.MCPClient("invalid")
        client.register([{"name": "bad", "inputSchema": {"type": "array"}}], {"bad": lambda: "bad"})
        mcp_tools.mcp_clients["invalid"] = client
    else:
        def fail_index():
            raise OSError("context_length_exceeded" if failure == "preparation_context_error" else "memory unavailable")

        monkeypatch.setattr(memory_runtime, "read_memory_index", fail_index)

    monkeypatch.setattr(compaction, "reactive_compact", lambda *args: pytest.fail("Preparation was treated as a model context error"))
    job = scheduler.schedule_job("* * * * *", "scheduled work", recurring=False, durable=False)
    scheduler._enqueue_due_job(job)
    history = [{"role": "user", "content": "go"}]
    host.agent_loop(history, "go")
    assert calls == []
    assert scheduler._cron_queue == [job]
    assert scheduler._scheduled_jobs[job.id].pending_delivery
    assert not any(message.get("role") == "assistant" and "[Error]" in str(message.get("content"))
                   for message in history)

    monkeypatch.setattr(memory_runtime, "read_memory_index", original_index)
    mcp_tools.mcp_clients.clear()
    host.agent_loop(history, "retry")
    assert calls == [True]
    assert scheduler._cron_queue == []
    assert job.id not in scheduler._scheduled_jobs
