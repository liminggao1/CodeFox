"""Creation options are validated before execution and billed per instance."""

import agent_harness.workspace.tasks as tasks
import agent_harness.agents.subagent as subagent_runtime
import agent_harness.core as core


from types import SimpleNamespace as S

import pytest


def optioned_reply(text="done", tokens=7, stop="end_turn", block=None):
    """子 agent 创建用例使用的响应，按 tokens 折算 input/output 用量。"""
    return S(content=[block or S(type="text", text=text)], stop_reason=stop,
             usage=S(input_tokens=tokens - 2, output_tokens=2))


@pytest.mark.parametrize("tools", ["bash", ["unknown"], ["spawn_subagent"],
                                   ["send_message"], ["claim_task"], [None]])
def test_invalid_tools_do_not_claim_or_request(host_factory, tools):
    host = host_factory()
    task = tasks.create_task("Untouched task")
    client = S(messages=S(create=lambda **kw: pytest.fail("must not request")))
    result = subagent_runtime.spawn_subagent("child", "Inspect", "once",
                                         tools=tools, client=client)
    assert result["status"] == "failed"
    assert result["usage"] == {"tokens": 0, "model_calls": 0}
    assert not subagent_runtime.active_subagents and not tasks.assignments
    assert tasks.load_task(task.id).status == "pending"


def test_injected_client_tools_and_usage_cover_multiple_rounds(host_factory):
    host = host_factory()
    requests = []
    responses = iter([optioned_reply(block=S(type="tool_use", name="write_file", id="write",
                                  input={"path": "result.txt", "content": "result"})),
                      optioned_reply(tokens=11)])
    def create(**kw):
        assert "child" in subagent_runtime.active_subagents
        requests.append(kw)
        return next(responses)
    client = S(messages=S(create=create))
    result = subagent_runtime.spawn_subagent("child", "Write", "once", tools=["write_file"],
                                         client=client, model="injected", max_tokens=123)
    assert result["status"] == "completed"
    assert result["usage"] == {"tokens": 18, "model_calls": 2}
    assert all(r["model"] == "injected" and r["max_tokens"] == 123 for r in requests)
    assert [t["name"] for t in requests[0]["tools"]] == ["write_file"]
    assert (core.WORKDIR / "result.txt").read_text() == "result"
    assert not subagent_runtime.active_subagents


@pytest.mark.parametrize("stop", ["end_turn", "max_tokens"])
def test_empty_tools_never_execute_hallucinated_call(host_factory, stop):
    host = host_factory()
    calls = []
    block = S(type="tool_use", name="write_file", id="write",
              input={"path": "forbidden.txt", "content": "no"})
    def create(**kw):
        calls.append(kw)
        return optioned_reply(block=block, stop=stop) if len(calls) == 1 else optioned_reply()
    result = subagent_runtime.spawn_subagent("child", "Read text", "once", tools=[],
                                         client=S(messages=S(create=create)))
    assert not calls[0].get("tools")
    assert not (core.WORKDIR / "forbidden.txt").exists()
    assert result["usage"] == {"tokens": 7 if stop == "max_tokens" else 14,
                               "model_calls": 1 if stop == "max_tokens" else 2}
    assert result["status"] == ("failed" if stop == "max_tokens" else "completed")


def test_usage_includes_summary_and_failed_request(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setattr(core, "COMPACT_TRIGGER", 30)
    monkeypatch.setattr(core, "COMPACT_TARGET", 20)
    requests = []
    def create(**kw):
        requests.append(kw)
        if "tools" not in kw:
            return optioned_reply("summary", tokens=9)
        raise RuntimeError("execution unavailable")
    result = subagent_runtime.spawn_subagent("child", "Long instruction " * 20, "once",
                                         client=S(messages=S(create=create)), model="injected",
                                         max_tokens=123)
    assert result["status"] == "failed"
    assert result["usage"] == {"tokens": 9, "model_calls": 2}
    assert requests[0]["max_tokens"] == core.COMPACTION_MAX_TOKENS
    assert all(r["model"] == "injected" for r in requests)
    assert not subagent_runtime.active_subagents
