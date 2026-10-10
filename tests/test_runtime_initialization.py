"""Application configuration is explicit and preserves caller-selected runners."""

import agent_harness.workflows.workflow_runtime as workflow_runtime


import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def run_isolated(code, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_and_initialization_preserve_injected_runner(tmp_path):
    run_isolated('''
        import importlib
        from agent_harness.workflows import workflow_runtime as workflow
        assert workflow.RUNNER_FACTORY is workflow.SubagentRunner
        chosen = lambda: None
        workflow.RUNNER_FACTORY = chosen
        import agent_harness.host as host
        import agent_harness.workflows.workflow_runtime as workflow_runtime
        assert workflow.RUNNER_FACTORY is chosen
        assert host.GOAL is None
        assert not workflow.WORKFLOWS
        assert host.assemble_tool_pool() == ([], {})
        host.initialize_runtime()
        assert workflow.RUNNER_FACTORY is chosen
        assert "review-changes" in workflow.WORKFLOWS
        assert "Workflow" in host.assemble_tool_pool()[1]
        goal = host.GOAL
        goal.set_goal("Keep the active goal")
        host.initialize_runtime()
        assert host.GOAL is goal and goal.active.condition == "Keep the active goal"
        assert workflow.RUNNER_FACTORY is chosen
        importlib.reload(host)
        assert workflow.RUNNER_FACTORY is chosen and host.GOAL is None
        host.initialize_runtime()
        assert workflow.RUNNER_FACTORY is chosen
    ''', tmp_path)


def test_failed_start_leaves_configuration_unpublished_and_can_retry(tmp_path):
    run_isolated('''
        import os
        import threading
        from pathlib import Path
        from agent_harness.runtime.interactive_runtime import InteractiveRuntime
        from agent_harness import host
        import agent_harness.workflows.workflow_runtime as workflow_runtime
        from agent_harness.workflows import workflow_runtime as workflow
        chosen = lambda: None
        workflow.RUNNER_FACTORY = chosen
        runtime = InteractiveRuntime()
        assert host.GOAL is None and not workflow.WORKFLOWS
        threads = set(threading.enumerate())
        os.environ["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "invalid"
        try:
            runtime.start()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid configuration should fail")
        assert runtime.submit("hello").reason == "Runtime is not started"
        assert set(threading.enumerate()) == threads
        assert host.GOAL is None and not workflow.WORKFLOWS
        assert host.assemble_tool_pool() == ([], {})
        assert workflow.RUNNER_FACTORY is chosen
        assert list(Path.cwd().iterdir()) == []
        os.environ["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "3"
        runtime.start()
        try:
            event = runtime.channel.next_event(timeout=3)
            assert event.type == "state_snapshot"
            assert host.GOAL is not None
            assert "Workflow" in host.assemble_tool_pool()[1]
            assert workflow.RUNNER_FACTORY is chosen
        finally:
            runtime.close()
            runtime._thread.join(timeout=5)
            assert not runtime._thread.is_alive()
    ''', tmp_path)


def test_explicit_goal_and_registry_survive_repeated_initialization(tmp_path):
    run_isolated('''
        import os
        from pathlib import Path
        from agent_harness import host
        import agent_harness.workflows.workflow_runtime as workflow_runtime
        from agent_harness.tools import tool_registry
        from agent_harness.workflows import workflow_runtime as workflow
        chosen = lambda: None
        workflow.RUNNER_FACTORY = chosen
        goal = host.configure_goal("chosen-evaluator")
        goal.set_goal("Keep this condition")
        os.environ["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "invalid"
        try:
            host.configure_goal()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid replacement should fail")
        assert host.GOAL is goal
        host.initialize_runtime()
        assert host.GOAL is goal and workflow.RUNNER_FACTORY is chosen
        entry = tool_registry.ToolDefinition({"name": "custom"}, lambda: "ok", "test", "deny")
        tool_registry.REGISTRY.configure(lambda: [entry])
        tool_registry.REGISTRY.snapshot()
        marker = object()
        workflow.WORKFLOWS["review-changes"] = marker
        host.initialize_runtime()
        assert host.GOAL is goal and goal.active.condition == "Keep this condition"
        assert workflow.RUNNER_FACTORY is chosen
        assert workflow.WORKFLOWS["review-changes"] is marker
        assert host.assemble_tool_pool()[1]["custom"]() == "ok"
        assert tool_registry.REGISTRY.policy_for("custom") == "deny"
        assert list(Path.cwd().iterdir()) == []
    ''', tmp_path)


@pytest.mark.parametrize("entry", ["cli", "tui"])
def test_frontend_import_does_not_configure_or_start_application(tmp_path, entry):
    if entry == "tui":
        pytest.importorskip("textual")
    # Block Textual for the CLI case so its optional dependency stays optional.
    block = '''
        class BlockTextual:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "textual" or fullname.startswith("textual."):
                    raise ModuleNotFoundError("Textual import blocked", name="textual")
        sys.meta_path.insert(0, BlockTextual())
    ''' if entry == "cli" else ""
    run_isolated(textwrap.dedent('''
        import importlib
        import sys
        import threading
        threads = set(threading.enumerate())
    ''') + textwrap.dedent(block) + f'''
importlib.import_module("agent_harness.frontends.{entry if entry == "cli" else "tui.app"}")
from agent_harness import host
import agent_harness.workflows.workflow_runtime as workflow_runtime
assert host.GOAL is None and not workflow_runtime.WORKFLOWS
assert host.assemble_tool_pool() == ([], {{}})
assert set(threading.enumerate()) == threads
''', tmp_path)
