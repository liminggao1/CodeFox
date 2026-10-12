"""Workflow 运行时：journal 回放、预算与 runner 接线，以及跨真实子 agent 接口的工作流步骤。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as S
import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time

import pytest

import agent_harness.agents.subagent as subagent_runtime
import agent_harness.core as core
import agent_harness.registrations as registrations
import agent_harness.runtime.hooks as hooks
import agent_harness.workflows.workflow_runtime as workflow_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]

from agent_harness.workflows import workflow_runtime as workflow  # noqa: E402
from agent_harness.workflows import workflow_demo


async def yield_to_loop():
    """让出事件循环若干次，使已排队的协程推进到工作流边界。"""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def deterministic_runner(monkeypatch):
    """Explicitly register the sample and select its offline runner."""
    workflow_demo.register_workflows()
    monkeypatch.setattr(workflow, "RUNNER_FACTORY",
                        lambda: workflow.SubagentRunner(workflow_demo.DemoModelClient()))


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
    from agent_harness.workflows import workflow_runtime as child_workflow

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


def test_core_runtime_runs_without_importing_demo(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src"))
    code = (
        "import asyncio, sys; from agent_harness.workflows import workflow_runtime as w; "
        "assert 'agent_harness.workflows.workflow_demo' not in sys.modules; "
        "assert not w.WORKFLOWS; "
        "w.RUNNER_FACTORY=lambda: None; "
        "exec('async def sample(ctx, args):\\n    return {\"answer\": 42}'); "
        "out=asyncio.run(w.WorkflowTool().call({'name':'standalone','description':'test'},sample)); "
        "assert out['task'].status=='completed'; assert out['result']=={'answer':42}"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("schema", [None, {"type": "object", "properties": {
    "answer": {"type": "string"}}, "required": ["answer"]}])
def test_truncated_runner_fails_without_caching_and_resume_calls_model(tmp_path, monkeypatch, schema):
    monkeypatch.setattr(workflow.core, "WORKDIR", tmp_path)
    calls = []
    responses = [SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])
                 for stop, text in [("max_tokens", '{"answer":"partial"}'),
                                    ("end_turn", '{"answer":"complete"}')]]

    def create(**kwargs):
        calls.append(True)
        return responses.pop(0)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(workflow, "RUNNER_FACTORY", lambda: workflow.SubagentRunner(client, "test"))

    async def step(ctx, args):
        return await ctx.agent("one step", schema=schema, label="first")

    meta = {"name": "truncated", "description": "test"}
    failed = asyncio.run(workflow.WorkflowTool().call(meta, step))
    run_id = failed["task"].run_id
    assert failed["task"].status == "failed"
    assert "truncated (max_tokens)" in failed["result"]["error"]
    journal = workflow.store_dir() / f"{run_id}.journal.jsonl"
    assert journal.read_text(encoding="utf-8") == ""
    assert calls == [True]

    resumed = asyncio.run(workflow.WorkflowTool().call(meta, step, resume_from_run_id=run_id))
    assert resumed["task"].status == "completed"
    assert resumed["result"] == ({"answer": "complete"} if schema else '{"answer":"complete"}')
    assert calls == [True, True]


def test_resume_replays_at_budget_limit_without_spending_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow.core, "WORKDIR", tmp_path)
    calls = []

    class CountingRunner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
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

    severity = workflow_demo.FINDINGS_SCHEMA["properties"]["findings"]["items"][
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
    assert budget.spent() == 2

    journal = workflow.WorkflowJournal(
        "wf_limit-test_0001", resume=False, store=tmp_path
    )
    task = workflow.LocalWorkflowTask("task", "wf_limit-test_0001", {})
    state = workflow.ExecutionState(
        task, journal, workflow.SubagentRunner(workflow_demo.DemoModelClient()), workflow.Budget(), {}
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
    assert result["result"]["confirmed"]
    assert all(f["dimension"] in workflow_demo.DIMENSIONS for f in result["result"]["confirmed"])
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


def test_subagent_runner_parses_json_and_records_real_usage() -> None:
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
    runner = workflow.SubagentRunner(client, "deepseek-v4-flash")

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
    assert {t["name"] for t in calls[0]["tools"]} == set(workflow.subagent.selected_tool_names(None))


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
    runner = workflow.SubagentRunner(client, "test-model")
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
        def run(self, prompt, schema=None, label=None, *, tools=None):
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
        def run(self, prompt, schema=None, label=None, *, tools=None):
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


def test_failed_parallel_waits_for_running_sibling_and_replays_success(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    started = threading.Event()
    release = threading.Event()
    failed = threading.Event()
    calls = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            if prompt == "late":
                started.set()
                assert release.wait(5)
                return workflow.RunnerOutput("saved", 5)
            assert started.wait(5)
            failed.set()
            raise workflow.WorkflowStepError("first failure", tokens=3)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)
    meta = {"name": "settlement", "description": "test"}

    async def script(ctx, _args):
        return await ctx.parallel([
            lambda: ctx.agent("fail"), lambda: ctx.agent("late")])

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(meta, script))
        try:
            assert await asyncio.to_thread(failed.wait, 5)
            # A loop barrier lets the failed branch reach the workflow boundary.
            await yield_to_loop()
            assert not pending.done()
        finally:
            release.set()
        result = await pending

        async def replay(ctx, _args):
            return await ctx.agent("late")

        resumed = await workflow.WorkflowTool().call(
            meta, replay, resume_from_run_id=result["task"].run_id)
        assert resumed["result"] == "saved"
        assert resumed["task"].usage == {"agents": 0, "tokens": 0}
        return result

    result = asyncio.run(exercise())
    assert result["result"] == {"error": "first failure"}
    assert result["task"].usage == {"agents": 2, "tokens": 8}
    assert calls == ["fail", "late"]


@pytest.mark.parametrize("business_error", [False, True])
def test_caught_budget_overrun_stays_failed_and_blocks_new_calls(tmp_path, monkeypatch, business_error):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    calls = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            if business_error:
                raise workflow.WorkflowStepError("runner failed", tokens=5)
            return workflow.RunnerOutput("done", 5)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def script(ctx, _args):
        with pytest.raises(Exception, match="runner failed" if business_error else "budget exceeded"):
            await ctx.agent("first")
        with pytest.raises(workflow.WorkflowInputError, match="budget exceeded"):
            await ctx.agent("second")
        return "caught"

    result = asyncio.run(workflow.WorkflowTool().call(
        {"name": "overrun", "description": "test"}, script, {"budget": 1}))
    assert calls == ["first"]
    assert result["task"].status == "failed"
    assert result["task"].usage == {"agents": 1, "tokens": 5}
    assert result["task"].cumulative_usage == {"agents": 1, "tokens": 5}
    assert "budget exceeded" in result["result"]["error"]


@pytest.mark.parametrize("early_failure", [False, True])
def test_script_exit_settles_registered_pipeline_including_later_stages(tmp_path, monkeypatch, early_failure):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []
    background = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            if prompt == "first":
                started.set()
                assert release.wait(5)
            return workflow.RunnerOutput(prompt, 2)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def script(ctx, _args):
        async def first(value, item, index):
            return await ctx.agent("first")

        async def second(value, item, index):
            return await ctx.agent("second")

        background.append(asyncio.create_task(ctx.pipeline([1], first, second)))
        assert await asyncio.to_thread(started.wait, 5)
        if early_failure:
            raise RuntimeError("script failed")
        return "script returned"

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(
            {"name": "early-exit", "description": "test"}, script))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await yield_to_loop()
            assert not pending.done()
        finally:
            release.set()
        result = await pending
        assert await background[0] == ["second"]
        return result

    result = asyncio.run(exercise())
    assert calls == ["first", "second"]
    assert result["task"].usage == {"agents": 2, "tokens": 4}
    assert result["task"].status == ("failed" if early_failure else "completed")


def test_cancel_settles_running_call_saves_failure_and_rethrows(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    started = threading.Event()
    release = threading.Event()
    run_id = "wf_cancel_0000000000000001"
    monkeypatch.setattr(workflow, "create_run_id", lambda meta: run_id)
    calls = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            started.set()
            assert release.wait(5)
            return workflow.RunnerOutput("done", 7)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)
    meta = {"name": "cancel", "description": "test"}

    async def script(ctx, _args):
        return await ctx.agent("running")

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(meta, script))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            pending.cancel("user stopped workflow")
            await yield_to_loop()
            pending.cancel()
            await yield_to_loop()
            assert not pending.done()
            with pytest.raises(workflow.WorkflowInputError, match="already active"):
                await workflow.WorkflowTool().call(meta, script, resume_from_run_id=run_id)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError, match="user stopped workflow"):
            await pending
        with workflow.workflow_run_lock(run_id):
            pass

    asyncio.run(exercise())
    snapshot = json.loads((tmp_path / f"{run_id}.json").read_text())
    assert snapshot["task"]["status"] == "failed"
    assert snapshot["task"]["usage"] == {"agents": 1, "tokens": 7}
    assert json.loads((tmp_path / f"{run_id}.output.json").read_text()) == {"error": "Workflow cancelled"}
    assert (tmp_path / f"{run_id}.journal.jsonl").read_text() == ""
    assert calls == ["running"]


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("same_key", [False, True])
def test_exit_tracks_steps_waiting_for_slot_or_key(tmp_path, monkeypatch, cancel, same_key):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(workflow, "CONCURRENCY", 1)
    started = threading.Event()
    release = threading.Event()
    waiting = asyncio.Event()
    children = []
    calls = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            if prompt == "first":
                started.set()
                assert release.wait(5)
            return workflow.RunnerOutput(prompt, 3)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def script(ctx, _args):
        children.append(asyncio.create_task(ctx.agent("first")))
        assert await asyncio.to_thread(started.wait, 5)

        async def queued():
            waiting.set()
            return await ctx.agent("first" if same_key else "second")

        children.append(asyncio.create_task(queued()))
        await waiting.wait()
        return "returned"

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(
            {"name": "waiters", "description": "test"}, script))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            await yield_to_loop()
            if cancel:
                pending.cancel()
                await yield_to_loop()
            assert not pending.done()
        finally:
            release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.gather(*children, return_exceptions=True)
            assert calls == ["first"]
        else:
            result = await pending
            await asyncio.gather(*children)
            assert calls == (["first"] if same_key else ["first", "second"])
            assert result["task"].usage == {
                "agents": 1 if same_key else 2, "tokens": 3 if same_key else 6}

    asyncio.run(exercise())


def test_factory_failure_settles_preceding_members_without_starting_later_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    calls = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            return workflow.RunnerOutput(prompt, 2)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def script(ctx, args):
        def fail_factory():
            raise RuntimeError("factory failed")

        return await ctx.parallel([
            lambda: ctx.agent("accepted"), fail_factory,
            lambda: ctx.agent("not accepted")])

    result = asyncio.run(workflow.WorkflowTool().call(
        {"name": "factory", "description": "test"}, script))
    assert result["result"] == {"error": "factory failed"}
    assert calls == ["accepted"]
    assert result["task"].usage == {"agents": 1, "tokens": 2}


def test_nested_group_finishes_during_close_and_independent_new_work_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []
    contexts = []
    children = []

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            if prompt == "first":
                started.set()
                assert release.wait(5)
            return workflow.RunnerOutput(prompt, 1)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def nested(ctx, args):
        await ctx.agent("first")
        return await ctx.agent("second")

    monkeypatch.setitem(workflow.WORKFLOWS, "nested", (
        {"name": "nested", "description": "test"}, nested))

    async def script(ctx, args):
        contexts.append(ctx)
        children.append(asyncio.create_task(ctx.workflow("nested")))
        assert await asyncio.to_thread(started.wait, 5)
        return "returned"

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(
            {"name": "parent", "description": "test"}, script))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await yield_to_loop()
            ctx = contexts[0]
            for operation in [ctx.agent("new"), ctx.parallel([]), ctx.pipeline([])]:
                with pytest.raises(workflow.WorkflowInputError, match="closing"):
                    await operation
        finally:
            release.set()
        result = await pending
        assert await children[0] == "second"
        return result

    result = asyncio.run(exercise())
    assert result["task"].usage == {"agents": 2, "tokens": 2}
    assert calls == ["first", "second"]


def test_handled_business_error_does_not_replace_later_script_error(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            raise workflow.WorkflowStepError("handled", tokens=3)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def script(ctx, args):
        try:
            await ctx.agent("one")
        except workflow.WorkflowStepError:
            pass
        raise RuntimeError("unhandled")

    result = asyncio.run(workflow.WorkflowTool().call(
        {"name": "errors", "description": "test"}, script))
    assert result["result"] == {"error": "unhandled"}
    assert result["task"].usage == {"agents": 1, "tokens": 3}


@pytest.mark.parametrize("primitive", ["parallel", "pipeline"])
def test_cancel_interrupts_group_members_waiting_before_agent(tmp_path, monkeypatch, primitive):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    entered = asyncio.Event()
    interrupted = asyncio.Event()
    release = asyncio.Event()

    async def wait_without_runner(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise

    async def script(ctx, args):
        if primitive == "parallel":
            return await ctx.parallel([wait_without_runner])
        return await ctx.pipeline([1], wait_without_runner)

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(
            {"name": "async-wait", "description": "test"}, script))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel("cancel async stage")
            await asyncio.wait_for(interrupted.wait(), 1)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError, match="cancel async stage"):
            await pending

    asyncio.run(exercise())


@pytest.mark.parametrize("budget", [None, 1])
def test_pipeline_failure_keeps_other_item_running_and_preserves_first_error(tmp_path, monkeypatch, budget):
    monkeypatch.setattr(workflow, "store_dir", lambda: tmp_path)
    started = threading.Event()
    failed = threading.Event()
    release = threading.Event()
    calls = []
    meta = {"name": "pipeline-error", "description": "test"}

    class Runner:
        def run(self, prompt, schema=None, label=None, *, tools=None):
            calls.append(prompt)
            if prompt == "fail":
                assert started.wait(5)
                failed.set()
                raise workflow.WorkflowStepError("first business error", tokens=3)
            if prompt == "late":
                started.set()
                assert release.wait(5)
            return workflow.RunnerOutput(prompt, 5)

    monkeypatch.setattr(workflow, "RUNNER_FACTORY", Runner)

    async def script(ctx, args):
        async def first(value, item, index):
            return await ctx.agent(item)

        async def second(value, item, index):
            return await ctx.agent(f"next {item}")

        return await ctx.pipeline(["fail", "late"], first, second)

    async def exercise():
        pending = asyncio.create_task(workflow.WorkflowTool().call(meta, script, {"budget": budget}))
        try:
            assert await asyncio.to_thread(failed.wait, 5)
            await yield_to_loop()
            assert not pending.done()
        finally:
            release.set()
        result = await pending
        if budget is None:
            async def replay(ctx, args):
                return await ctx.agent("late")
            resumed = await workflow.WorkflowTool().call(
                meta, replay, resume_from_run_id=result["task"].run_id)
            assert resumed["result"] == "late"
            assert resumed["task"].usage == {"agents": 0, "tokens": 0}
        else:
            path = tmp_path / f"{result['task'].run_id}.journal.jsonl"
            assert path.read_text() == ""
            resumed = await workflow.WorkflowTool().call(
                meta, script, resume_from_run_id=result["task"].run_id)
            assert resumed["task"].status == "failed"
            assert resumed["task"].usage == {"agents": 0, "tokens": 0}
        return result

    result = asyncio.run(exercise())
    assert result["result"] == {"error": "first business error"}
    assert result["task"].usage == {"agents": 2 if budget else 3, "tokens": 8 if budget else 13}
    assert calls == (["fail", "late"] if budget else ["fail", "late", "next late"])


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

    assert names == [tool["name"] for tool in registrations.BUILTIN_TOOLS] + ["Workflow"]
    assert handlers["Workflow"] is workflow_runtime.run_workflow_sync
    assert handlers["Workflow"](name="missing").content == (
        "Error: unknown workflow 'missing'"
    )


def test_default_workflow_runner_uses_current_client_and_model(host_factory, monkeypatch) -> None:
    host = host_factory()
    monkeypatch.setattr(workflow, "RUNNER_FACTORY", workflow.SubagentRunner)
    monkeypatch.setattr(core, "client", object())
    monkeypatch.setattr(core, "MODEL", "current-model")
    runner = workflow_runtime.RUNNER_FACTORY()

    assert isinstance(runner, workflow.SubagentRunner)
    assert runner.client is core.client
    assert runner.model == core.MODEL


OK_SCHEMA = {"type": "object", "required": ["ok"],
             "properties": {"ok": {"type": "boolean"}}}


def workflow_reply(value="done", tokens=5, block=None, stop="end_turn"):
    """工作流子 agent 用例使用的响应；value 为字典时序列化为 JSON 文本。"""
    return S(content=[block or S(type="text", text=value if isinstance(value, str) else json.dumps(value))],
             stop_reason=stop, usage=S(input_tokens=tokens - 1, output_tokens=1))


def install(host, monkeypatch, create):
    wf = workflow_runtime
    client = S(messages=S(create=create))
    monkeypatch.setattr(wf, "RUNNER_FACTORY", lambda: wf.SubagentRunner(client, "injected"))
    return wf


def test_actual_workflow_tool_executes_child_tools_and_replays(host_factory, monkeypatch, no_approval):
    host = host_factory()
    globals_before = (core.client, core.MODEL, core.DEFAULT_MAX_TOKENS)
    calls = []
    responses = iter([workflow_reply(tokens=7, block=S(type="tool_use", id="w", name="write_file",
                          input={"path": "workflow.txt", "content": "saved"})), workflow_reply(tokens=11)])
    def create(**kw):
        assert len(subagent_runtime._active_subagents) == 1
        record = next(iter(subagent_runtime._active_subagents.values()))
        assert record.mode == "once" and record.role == "workflow"
        assert subagent_runtime.is_valid_agent_name(record.name)
        calls.append(kw)
        return next(responses)
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Save the result", label="写入：结果")
    monkeypatch.setitem(wf.WORKFLOWS, "child-tools", ({"name": "child-tools", "description": "test"}, step))
    handler = host.assemble_tool_pool()[1]["Workflow"]
    first = json.loads(handler(name="child-tools").content)
    resumed = json.loads(handler(name="child-tools", resume_from_run_id=first["task"]["runId"]).content)
    assert first["task"]["status"] == "completed"
    assert first["task"]["usage"] == {"agents": 1, "tokens": 18}
    assert resumed["task"]["usage"] == {"agents": 0, "tokens": 0}
    assert len(calls) == 2 and not subagent_runtime._active_subagents
    assert {t["name"] for t in calls[0]["tools"]} == set(subagent_runtime.selected_tool_names(None))
    assert all(r["model"] == "injected" and r["max_tokens"] == core.WORKFLOW_AGENT_MAX_TOKENS
               for r in calls)
    assert (core.WORKDIR / "workflow.txt").read_text() == "saved"
    assert (core.client, core.MODEL, core.DEFAULT_MAX_TOKENS) == globals_before


@pytest.mark.parametrize("tools", [[], ["read_file"]])
def test_workflow_tool_restriction_blocks_unavailable_writes(host_factory, monkeypatch, tools):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        return workflow_reply(block=S(type="tool_use", id="w", name="write_file",
                            input={"path": "forbidden.txt", "content": "no"})) if len(calls) == 1 else workflow_reply()
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Analyze", tools=tools)
    out = asyncio.run(wf.WorkflowTool().call({"name": "restricted", "description": "test"}, step))
    assert out["task"].status == "completed"
    assert [t["name"] for t in calls[0].get("tools", [])] == tools
    assert not (core.WORKDIR / "forbidden.txt").exists()
    assert "unavailable" in str(calls[1]["messages"])


def test_format_repair_does_not_repeat_file_operations(host_factory, monkeypatch, no_approval):
    host = host_factory()
    calls = []
    responses = iter([workflow_reply(tokens=3, block=S(type="tool_use", id="w", name="write_file",
                          input={"path": "once.txt", "content": "saved"})),
                      workflow_reply("invalid result", tokens=5), workflow_reply({"ok": True}, tokens=7)])
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
    assert (core.WORKDIR / "once.txt").read_text() == "saved"
    assert not subagent_runtime._active_subagents


@pytest.mark.parametrize("failure", ["truncated", "exception", "invalid_json"])
def test_failed_step_is_billed_without_cache_and_resume_keeps_usage(host_factory, monkeypatch, failure):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        if failure == "exception":
            if len(calls) == 1:
                return workflow_reply(tokens=5, block=S(type="tool_use", id="r", name="glob", input={"pattern": "*"}))
            raise RuntimeError("model unavailable")
        return workflow_reply("invalid result", tokens=5, stop="max_tokens" if failure == "truncated" else "end_turn")
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
    assert not subagent_runtime._active_subagents


def test_tools_are_part_of_the_cache_identity(host_factory, monkeypatch):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        return workflow_reply()
    wf = install(host, monkeypatch, create)
    tools = None
    async def step(ctx, args):
        return await ctx.agent("Same instructions", label="same", tools=tools)
    meta = {"name": "tool-cache", "description": "test"}
    first = asyncio.run(wf.WorkflowTool().call(meta, step))
    tools = list(reversed(subagent_runtime.selected_tool_names(None)))
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
            return workflow_reply({"ok": True} if "tools" not in kw else "invalid result")
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
    assert not subagent_runtime._active_subagents


@pytest.mark.parametrize("budget", [1, 5])
def test_overspend_or_exhaustion_blocks_format_retry_and_records_consumption(host_factory, monkeypatch, budget):
    host = host_factory()
    calls = []
    def create(**kw):
        calls.append(kw)
        return workflow_reply("invalid result", tokens=5)
    wf = install(host, monkeypatch, create)
    async def step(ctx, args):
        return await ctx.agent("Analyze", schema=OK_SCHEMA)
    meta = {"name": "budget-failure", "description": "test"}
    first = asyncio.run(wf.WorkflowTool().call(meta, step, {"budget": budget}))
    assert first["task"].status == "failed"
    assert first["task"].usage == {"agents": 1, "tokens": 5}
    expected = "invalid output" if budget == 1 else "token budget exceeded"
    assert expected in first["result"]["error"]
    resumed = asyncio.run(wf.WorkflowTool().call(meta, step, resume_from_run_id=first["task"].run_id))
    assert resumed["task"].usage == {"agents": 0, "tokens": 0}
    assert len(calls) == 1 and not subagent_runtime._active_subagents


def test_invalid_workflow_tools_fail_without_child_or_model(host_factory, monkeypatch):
    host = host_factory()
    wf = install(host, monkeypatch, lambda **kw: pytest.fail("must not request"))
    async def step(ctx, args):
        return await ctx.agent("Inspect", tools=["send_message"])
    out = asyncio.run(wf.WorkflowTool().call({"name": "bad-tools", "description": "test"}, step))
    assert out["task"].status == "failed" and out["task"].usage == {"agents": 0, "tokens": 0}
    assert not subagent_runtime._active_subagents


def test_workflow_write_rejection_does_not_execute_or_run_post_hook(host_factory, monkeypatch):
    host = host_factory()
    events = []
    original_hooks = hooks.trigger_hooks
    def traced_hooks(event, block, *args):
        if event in {"PreToolUse", "PostToolUse"}:
            events.append(event)
            if event == "PreToolUse":
                return "Denied by test hook"
        return original_hooks(event, block, *args)
    monkeypatch.setattr(hooks, "trigger_hooks", traced_hooks)
    responses = iter([workflow_reply(block=S(type="tool_use", id="w", name="write_file",
                                  input={"path": "denied.txt", "content": "no"})), workflow_reply()])
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
    assert not (core.WORKDIR / "denied.txt").exists()
