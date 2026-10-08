"""Workflow steps cross the real child interface with fake model responses."""

import asyncio
import json
import threading
from types import SimpleNamespace as S

import pytest


OK_SCHEMA = {"type": "object", "required": ["ok"],
             "properties": {"ok": {"type": "boolean"}}}


def reply(value="done", tokens=5, block=None, stop="end_turn"):
    return S(content=[block or S(type="text", text=value if isinstance(value, str) else json.dumps(value))],
             stop_reason=stop, usage=S(input_tokens=tokens - 1, output_tokens=1))


def install(host, monkeypatch, create):
    wf = host.workflow_runtime
    client = S(messages=S(create=create))
    monkeypatch.setattr(wf, "RUNNER_FACTORY", lambda: wf.SubagentRunner(client, "injected"))
    return wf


def test_actual_workflow_tool_executes_child_tools_and_replays(host_factory, monkeypatch):
    host = host_factory()
    globals_before = (host.core.client, host.core.MODEL, host.core.DEFAULT_MAX_TOKENS)
    calls = []
    responses = iter([reply(tokens=7, block=S(type="tool_use", id="w", name="write_file",
                          input={"path": "workflow.txt", "content": "saved"})), reply(tokens=11)])
    def create(**kw):
        assert len(host.active_subagents) == 1
        record = next(iter(host.active_subagents.values()))
        assert record.mode == "once" and record.role == "workflow"
        assert host.is_valid_agent_name(record.name)
        calls.append(kw)
        return next(responses)
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Save the result", label="写入：结果")
    monkeypatch.setitem(wf.WORKFLOWS, "child-tools", ({"name": "child-tools", "description": "test"}, step))
    handler = host.assemble_tool_pool()[1]["Workflow"]
    first = json.loads(handler(name="child-tools"))
    resumed = json.loads(handler(name="child-tools", resume_from_run_id=first["task"]["runId"]))
    assert first["task"]["status"] == "completed"
    assert first["task"]["usage"] == {"agents": 1, "tokens": 18}
    assert resumed["task"]["usage"] == {"agents": 0, "tokens": 0}
    assert len(calls) == 2 and not host.active_subagents
    assert {t["name"] for t in calls[0]["tools"]} == set(host.subagent.selected_tool_names(None))
    assert all(r["model"] == "injected" and r["max_tokens"] == host.core.WORKFLOW_AGENT_MAX_TOKENS
               for r in calls)
    assert (host.core.WORKDIR / "workflow.txt").read_text() == "saved"
    assert (host.core.client, host.core.MODEL, host.core.DEFAULT_MAX_TOKENS) == globals_before


@pytest.mark.parametrize("tools", [[], ["read_file"]])
def test_workflow_tool_restriction_blocks_unavailable_writes(host_factory, monkeypatch, tools):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        return reply(block=S(type="tool_use", id="w", name="write_file",
                            input={"path": "forbidden.txt", "content": "no"})) if len(calls) == 1 else reply()
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Analyze", tools=tools)
    out = asyncio.run(wf.WorkflowTool().call({"name": "restricted", "description": "test"}, step))
    assert out["task"].status == "completed"
    assert [t["name"] for t in calls[0].get("tools", [])] == tools
    assert not (host.core.WORKDIR / "forbidden.txt").exists()
    assert "unavailable" in str(calls[1]["messages"])


def test_format_repair_does_not_repeat_file_operations(host_factory, monkeypatch):
    host = host_factory()
    calls = []
    responses = iter([reply(tokens=3, block=S(type="tool_use", id="w", name="write_file",
                          input={"path": "once.txt", "content": "saved"})),
                      reply("invalid result", tokens=5), reply({"ok": True}, tokens=7)])
    def create(**kw):
        calls.append(kw)
        return next(responses)
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Perform the work", schema=OK_SCHEMA)
    out = asyncio.run(wf.WorkflowTool().call({"name": "repair", "description": "test"}, step))
    assert out["result"] == {"ok": True}
    assert out["task"].usage == {"agents": 1, "tokens": 15}
    assert "tools" not in calls[2]
    assert "invalid result" in calls[2]["messages"][0]["content"]
    assert "Correct only" in calls[2]["messages"][0]["content"]
    assert (host.core.WORKDIR / "once.txt").read_text() == "saved"
    assert not host.active_subagents


@pytest.mark.parametrize("failure", ["truncated", "exception", "invalid_json"])
def test_failed_step_is_billed_without_cache_and_resume_keeps_usage(host_factory, monkeypatch, failure):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        if failure == "exception":
            if len(calls) == 1:
                return reply(tokens=5, block=S(type="tool_use", id="r", name="glob", input={"pattern": "*"}))
            raise RuntimeError("model unavailable")
        return reply("invalid result", tokens=5, stop="max_tokens" if failure == "truncated" else "end_turn")
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Perform", schema=OK_SCHEMA)
    total = 10 if failure == "invalid_json" else 5
    out = asyncio.run(wf.WorkflowTool().call({"name": "failure", "description": "test"}, step, {"budget": total}))
    assert out["task"].status == "failed"
    assert out["task"].usage == {"agents": 1, "tokens": total}
    assert len(calls) == (1 if failure == "truncated" else 2)
    journal = wf.store_dir() / f"{out['task'].run_id}.journal.jsonl"
    assert journal.read_text() == ""
    resumed = asyncio.run(wf.WorkflowTool().call({"name": "failure", "description": "test"}, step,
                                               resume_from_run_id=out["task"].run_id))
    assert resumed["task"].status == "failed"
    assert resumed["task"].usage == {"agents": 0, "tokens": 0}
    assert resumed["task"].cumulative_usage == {"agents": 1, "tokens": total}
    assert not host.active_subagents


def test_tools_are_part_of_the_cache_identity(host_factory, monkeypatch):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        return reply()
    wf = install(host, monkeypatch, create)
    tools = None
    async def step(ctx, args):
        return await ctx.agent("Same instructions", label="same", tools=tools)
    meta = {"name": "tool-cache", "description": "test"}
    first = asyncio.run(wf.WorkflowTool().call(meta, step))
    tools = list(reversed(host.subagent.selected_tool_names(None)))
    cached = asyncio.run(wf.WorkflowTool().call(meta, step, resume_from_run_id=first["task"].run_id))
    assert cached["task"].usage == {"agents": 0, "tokens": 0}
    tools = []
    uncached = asyncio.run(wf.WorkflowTool().call(meta, step, resume_from_run_id=first["task"].run_id))
    assert uncached["task"].usage == {"agents": 1, "tokens": 5}
    assert len(calls) == 2 and "tools" not in calls[1]


def test_parallel_format_retries_respect_concurrency_and_unique_names(host_factory, monkeypatch):
    host = host_factory()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = maximum = 0
    names = set()
    def create(**kw):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            name = kw["system"].split("'")[1]
            assert name not in names
            names.add(name)
        try:
            barrier.wait(timeout=5)
            return reply({"ok": True} if "tools" not in kw else "invalid result")
        finally:
            with lock:
                active -= 1
    wf = install(host, monkeypatch, create)
    monkeypatch.setattr(wf, "CONCURRENCY", 2)
    async def step(ctx, args):
        return await ctx.parallel([lambda i=i: ctx.agent(f"Inspect {i}", schema=OK_SCHEMA, label="同名")
                                   for i in range(4)])
    out = asyncio.run(wf.WorkflowTool().call({"name": "parallel", "description": "test"}, step))
    assert out["task"].status == "completed", out["result"]
    assert maximum == 2 and len(names) == 8
    assert out["task"].usage == {"agents": 4, "tokens": 40}
    assert not host.active_subagents


@pytest.mark.parametrize("budget", [1, 5])
def test_overspend_or_exhaustion_blocks_format_retry_and_records_consumption(host_factory, monkeypatch, budget):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        return reply("invalid result", tokens=5)
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Analyze", schema=OK_SCHEMA)
    meta = {"name": "budget-failure", "description": "test"}
    first = asyncio.run(wf.WorkflowTool().call(meta, step, {"budget": budget}))
    assert first["task"].status == "failed"
    assert first["task"].usage == {"agents": 1, "tokens": 5}
    assert "token budget exceeded" in first["result"]["error"]
    resumed = asyncio.run(wf.WorkflowTool().call(meta, step, resume_from_run_id=first["task"].run_id))
    assert resumed["task"].usage == {"agents": 0, "tokens": 0}
    assert len(calls) == 1 and not host.active_subagents


def test_invalid_workflow_tools_fail_without_child_or_model(host_factory, monkeypatch):
    host = host_factory()
    wf = install(host, monkeypatch, lambda **kw: pytest.fail("must not request"))
    async def step(ctx, args):
        return await ctx.agent("Inspect", tools=["send_message"])
    out = asyncio.run(wf.WorkflowTool().call({"name": "bad-tools", "description": "test"}, step))
    assert out["task"].status == "failed" and out["task"].usage == {"agents": 0, "tokens": 0}
    assert not host.active_subagents


def test_workflow_write_rejection_does_not_execute_or_run_post_hook(host_factory, monkeypatch):
    host = host_factory()
    events = []
    original_hooks = host.hooks.trigger_hooks
    def hooks(event, block, *args):
        if event in {"PreToolUse", "PostToolUse"}:
            events.append(event)
            if event == "PreToolUse":
                return "Denied by test hook"
        return original_hooks(event, block, *args)
    monkeypatch.setattr(host.hooks, "trigger_hooks", hooks)
    responses = iter([reply(block=S(type="tool_use", id="w", name="write_file",
                                  input={"path": "denied.txt", "content": "no"})), reply()])
    calls = []
    def create(**kw):
        calls.append(kw)
        return next(responses)
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Write")
    out = asyncio.run(wf.WorkflowTool().call({"name": "deny", "description": "test"}, step))
    assert out["task"].status == "completed"
    assert events == ["PreToolUse"]
    assert "Denied by test hook" in str(calls[1]["messages"])
    assert not (host.core.WORKDIR / "denied.txt").exists()
