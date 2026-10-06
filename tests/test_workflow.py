"""Workflow 运行时测试。

Workflow 工具属于 host 工具池，因此这些用例检查真实工具池及 runner 连接。
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from agent_harness import workflow  # noqa: E402


@pytest.fixture(autouse=True)
def deterministic_runner(monkeypatch):
    """导入 agent.host 会连接 API runner；这些测试保持离线。"""
    monkeypatch.setattr(workflow, "RUNNER_FACTORY", workflow.MockAgentRunner)


def run_cli(*args: str, cwd: Path) -> str:
    env = dict(os.environ)
    path_entries = [str(PROJECT_ROOT / "src")]
    if env.get("PYTHONPATH"):
        path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path_entries)
    result = subprocess.run(
        [sys.executable, "-m", "agent_harness", "workflow", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def acquire_lock_in_child(run_id: str, store: str, results) -> None:
    from agent_harness import workflow as child_workflow

    child_workflow.store_dir = lambda: Path(store)
    try:
        with child_workflow.workflow_run_lock(run_id):
            results.put("acquired")
    except child_workflow.WorkflowInputError as exc:
        results.put(str(exc))


def test_workflow_runtime_resumes_from_journal(tmp_path: Path) -> None:
    first = run_cli("demo", cwd=tmp_path)
    resumed = run_cli("resume", cwd=tmp_path)

    assert "status=completed" in first
    assert "status=cached" in resumed
    assert "status=completed  agents=0  tokens=0" in resumed
    assert (tmp_path / ".runtime" / "last_run.txt").exists()


def test_resume_replays_at_budget_limit_without_spending_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow.core, "WORKDIR", tmp_path)
    calls = []

    class CountingRunner:
        def run(self, prompt, schema=None, label=None):
            calls.append(prompt)
            return workflow.RunnerOutput(prompt, 1)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", CountingRunner)
    meta = {"name": "budget-replay", "description": "test replay"}

    async def one_call(ctx, _args):
        return await ctx.agent("first", label="first")

    async def two_calls(ctx, _args):
        await ctx.agent("first", label="first")
        return await ctx.agent("second", label="second")

    first = asyncio.run(workflow.WorkflowTool().call(
        meta, one_call, {"budget": 1}
    ))
    run_id = first["task"].run_id
    replay = asyncio.run(workflow.WorkflowTool().call(
        meta, one_call, resume_from_run_id=run_id
    ))
    blocked = asyncio.run(workflow.WorkflowTool().call(
        meta, two_calls, resume_from_run_id=run_id
    ))

    assert calls == ["first"]
    assert replay["task"].status == "completed"
    assert replay["task"].usage == {"agents": 0, "tokens": 0}
    assert replay["task"].cumulative_usage == {"agents": 1, "tokens": 1}
    assert blocked["task"].status == "failed"
    assert "token budget exceeded" in blocked["result"]["error"]


def test_resume_rejects_journal_without_token_usage(tmp_path: Path) -> None:
    run_id = "wf_legacy_0001"
    journal_path = tmp_path / f"{run_id}.journal.jsonl"
    legacy_record = '{"key": "agent-old", "value": "done"}\n'
    journal_path.write_text(legacy_record, encoding="utf-8")

    with pytest.raises(workflow.WorkflowInputError, match="line 1"):
        workflow.WorkflowJournal(run_id, resume=True, store=tmp_path)

    assert journal_path.read_text(encoding="utf-8") == legacy_record


def test_workflow_runtime_rejects_unsafe_artifact_names() -> None:
    for name in ("../escape", "../../escape", "nested/name"):
        with pytest.raises(workflow.WorkflowInputError):
            workflow.validate_meta({"name": name, "description": "unsafe"})

    severity = workflow.FINDINGS_SCHEMA["properties"]["findings"]["items"][
        "properties"
    ]["severity"]
    validator = workflow.SimpleJsonSchema(severity)
    assert validator.validate("high") == (True, None)
    assert validator.validate("warning")[0] is False


def test_workflow_runtime_enforces_budget_and_shared_agent_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = workflow.Budget(total=1)
    with pytest.raises(workflow.WorkflowInputError):
        budget.add(2)
    assert budget.spent() == 0

    journal = workflow.WorkflowJournal(
        "wf_limit-test_0001", resume=False, store=tmp_path
    )
    task = workflow.LocalWorkflowTask("task", "wf_limit-test_0001", {})
    state = workflow.ExecutionState(
        task, journal, workflow.MockAgentRunner(), workflow.Budget(), {}
    )

    async def child(child_state, _args):
        return await child_state.agent("second call")

    monkeypatch.setattr(workflow, "AGENT_CAP", 1)
    monkeypatch.setitem(
        workflow.WORKFLOWS,
        "limit-child",
        ({"name": "limit-child", "description": "test"}, child),
    )

    async def run() -> None:
        await state.agent("first call")
        with pytest.raises(workflow.WorkflowInputError):
            await state.workflow("limit-child")

        async def fail_stage(_value, _item, _index):
            raise RuntimeError("stage failed")

        with pytest.raises(RuntimeError, match="stage failed"):
            await state.pipeline(["item"], fail_stage)

    try:
        asyncio.run(run())
    finally:
        journal.close()


def test_workflow_runtime_rejects_corrupt_resume_journal(tmp_path: Path) -> None:
    run_id = "wf_corrupt_0001"
    (tmp_path / f"{run_id}.journal.jsonl").write_text("{not-json}\n")

    with pytest.raises(workflow.WorkflowInputError, match="line 1"):
        workflow.WorkflowJournal(run_id, resume=True, store=tmp_path)


def test_workflow_tool_adapter_uses_registry_and_returns_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)

    result = asyncio.run(
        workflow.WORKFLOW_HANDLERS["Workflow"](
            name="review-changes", args={"budget": None}
        )
    )

    assert workflow.WORKFLOW_TOOL["input_schema"]["required"] == ["name"]
    assert result["launched"]["workflowName"] == "review-changes"
    assert result["task"]["status"] == "completed"
    assert result["task"]["taskType"] == "local_workflow"
    assert len(result["result"]["confirmed"]) == 5
    snapshot = json.loads(
        (tmp_path / f"{result['task']['runId']}.json").read_text()
    )
    assert snapshot["workflowName"] == "review-changes"
    assert snapshot["args"] == {"budget": None}
    assert snapshot["task"]["status"] == "completed"

    json.dumps(result)


def test_fresh_workflow_runs_have_unique_identity_and_resume_validates_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)

    first = asyncio.run(workflow.run_workflow("review-changes", {"budget": None}))
    second = asyncio.run(workflow.run_workflow("review-changes", {"budget": None}))

    assert first["task"]["runId"] != second["task"]["runId"]
    assert first["task"]["taskId"] != second["task"]["taskId"]
    with pytest.raises(workflow.WorkflowInputError, match="args do not match"):
        asyncio.run(
            workflow.run_workflow(
                "review-changes",
                {"budget": 1},
                resume_from_run_id=first["task"]["runId"],
            )
        )


def test_fresh_workflow_run_refuses_an_existing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    fixed_id = "wf_review-changes_0000000000001a7b"
    monkeypatch.setattr(workflow, "create_run_id", lambda _meta: fixed_id)

    first = asyncio.run(workflow.run_workflow("review-changes", {"budget": None}))
    first_snapshot = (tmp_path / f"{fixed_id}.json").read_text()
    first_output = (tmp_path / f"{fixed_id}.output.json").read_text()

    with pytest.raises(workflow.WorkflowInputError, match="unique workflow runId"):
        asyncio.run(workflow.run_workflow("review-changes", {"budget": None}))

    assert first["task"]["runId"] == fixed_id
    assert (tmp_path / f"{fixed_id}.json").read_text() == first_snapshot
    assert (tmp_path / f"{fixed_id}.output.json").read_text() == first_output


def test_invalid_resume_does_not_overwrite_completed_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    result = asyncio.run(workflow.run_workflow("review-changes", {"budget": None}))
    run_id = result["task"]["runId"]
    snapshot_path = tmp_path / f"{run_id}.json"
    output_path = tmp_path / f"{run_id}.output.json"
    journal_path = tmp_path / f"{run_id}.journal.jsonl"
    snapshot = snapshot_path.read_text()
    output = output_path.read_text()
    journal_path.write_text("not-json\n")

    with pytest.raises(workflow.WorkflowInputError, match="invalid resume journal"):
        asyncio.run(
            workflow.run_workflow("review-changes", resume_from_run_id=run_id)
        )

    assert snapshot_path.read_text() == snapshot
    assert output_path.read_text() == output


def test_active_workflow_run_rejects_concurrent_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    run_id = "wf_slow-test_0000000000001a7b"
    monkeypatch.setattr(workflow, "create_run_id", lambda _meta: run_id)
    started = asyncio.Event()
    release = asyncio.Event()
    meta = {"name": "slow-test", "description": "hold the run open"}

    async def slow_workflow(_ctx, _args):
        started.set()
        await release.wait()
        return {"invocation": 1}

    async def exercise():
        first = asyncio.create_task(
            workflow.WorkflowTool().call(meta, slow_workflow)
        )
        await started.wait()
        try:
            with pytest.raises(workflow.WorkflowInputError, match="already active"):
                await workflow.WorkflowTool().call(
                    meta, slow_workflow, resume_from_run_id=run_id
                )
        finally:
            release.set()
        return await first

    result = asyncio.run(exercise())

    assert result["result"] == {"invocation": 1}
    assert json.loads((tmp_path / f"{run_id}.output.json").read_text()) == {
        "invocation": 1
    }


def test_workflow_run_lock_is_cross_process(tmp_path: Path) -> None:
    original = workflow.store_dir
    workflow.store_dir = lambda: tmp_path
    run_id = "wf_process-lock_0000000000001a7b"
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    try:
        with workflow.workflow_run_lock(run_id):
            child = context.Process(
                target=acquire_lock_in_child,
                args=(run_id, str(tmp_path), results),
            )
            child.start()
            child.join(10)
    finally:
        workflow.store_dir = original

    assert child.exitcode == 0
    assert "already active" in results.get(timeout=1)


def test_anthropic_runner_parses_json_and_records_real_usage() -> None:
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="text", text='```json\n{"ok": true}\n```'
            )],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    runner = workflow.AnthropicAgentRunner(client, "deepseek-v4-flash")

    result = runner.run(
        "Check the supplied change.",
        schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        label="check",
    )

    assert result.value == {"ok": True}
    assert result.tokens == 18
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert "tools" not in calls[0]


def test_real_runner_output_retries_once_after_invalid_json(tmp_path: Path) -> None:
    responses = iter([
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="not json")],
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        ),
        SimpleNamespace(
            content=[SimpleNamespace(
                type="text", text='Result:\n```json\n{"ok": true}\n```\nDone.'
            )],
            usage=SimpleNamespace(input_tokens=4, output_tokens=3),
        ),
    ])
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: next(responses))
    )
    runner = workflow.AnthropicAgentRunner(client, "test-model")
    journal = workflow.WorkflowJournal(
        "wf_json-retry_0001", resume=False, store=tmp_path
    )
    task = workflow.LocalWorkflowTask("task", "wf_json-retry_0001", {})
    state = workflow.ExecutionState(
        task, journal, runner, workflow.Budget(), {}
    )

    try:
        result = asyncio.run(state.agent(
            "Return a result.",
            schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            label="json-retry",
        ))
    finally:
        journal.close()

    assert result == {"ok": True}
    assert task.usage == {"agents": 1, "tokens": 12}


def test_parallel_agent_calls_do_not_block_the_event_loop(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    class BarrierRunner:
        def run(self, prompt, schema=None, label=None):
            barrier.wait(timeout=5)
            return workflow.RunnerOutput({"label": label}, 1)

    journal = workflow.WorkflowJournal(
        "wf_parallel-test_0001", resume=False, store=tmp_path
    )
    task = workflow.LocalWorkflowTask("task", "wf_parallel-test_0001", {})
    state = workflow.ExecutionState(
        task, journal, BarrierRunner(), workflow.Budget(), {}
    )

    async def run():
        return await state.parallel([
            lambda: state.agent("first", label="first"),
            lambda: state.agent("second", label="second"),
        ])

    try:
        result = asyncio.run(run())
    finally:
        journal.close()

    assert result == [{"label": "first"}, {"label": "second"}]
    assert task.usage == {"agents": 2, "tokens": 2}


def test_parallel_identical_agent_calls_share_one_result(tmp_path: Path) -> None:
    calls = []

    class SlowRunner:
        def run(self, prompt, schema=None, label=None):
            calls.append(prompt)
            time.sleep(0.05)
            return workflow.RunnerOutput("shared", 2)

    journal = workflow.WorkflowJournal(
        "wf_same-key_0001", resume=False, store=tmp_path
    )
    task = workflow.LocalWorkflowTask("task", "wf_same-key_0001", {})
    state = workflow.ExecutionState(
        task, journal, SlowRunner(), workflow.Budget(2), {}
    )

    async def run():
        return await state.parallel([
            lambda: state.agent("same", label="same"),
            lambda: state.agent("same", label="same"),
        ])

    try:
        result = asyncio.run(run())
    finally:
        journal.close()

    assert result == ["shared", "shared"]
    assert calls == ["same"]
    assert task.usage == {"agents": 1, "tokens": 2}


def test_workflow_tool_adapter_rejects_model_supplied_code() -> None:
    properties = workflow.WORKFLOW_TOOL["input_schema"]["properties"]

    assert set(properties) == {"name", "args", "resume_from_run_id"}
    assert "description" not in properties
    assert "script" not in properties
    with pytest.raises(workflow.WorkflowInputError, match="name must be a string"):
        asyncio.run(workflow.run_workflow({"name": "review-changes"}))
    with pytest.raises(workflow.WorkflowInputError, match="unknown workflow"):
        asyncio.run(workflow.run_workflow("missing"))


def test_host_tool_pool_ships_the_workflow_tool(host_factory) -> None:
    host = host_factory()
    tools, handlers = host.assemble_tool_pool()
    names = [tool["name"] for tool in tools]

    assert names == [tool["name"] for tool in host.BUILTIN_TOOLS] + ["Workflow"]
    assert handlers["Workflow"] is host.workflow_runtime.run_workflow_sync
    assert handlers["Workflow"](name="missing") == (
        "Error: unknown workflow 'missing'"
    )


def test_host_wires_the_workflow_runner_to_its_own_client(host_factory) -> None:
    host = host_factory()

    runner = host.workflow_runtime.RUNNER_FACTORY()

    assert isinstance(runner, workflow.AnthropicAgentRunner)
    assert runner.client is host.core.client
    assert runner.model == host.core.MODEL
