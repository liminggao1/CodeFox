"""测试共用配置：项目导入路径、离线环境默认值和 host 工厂。"""

from __future__ import annotations



import os
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# 即使没有本地 .env，也保证导入过程离线且确定。
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("LLM_API", "responses")


def _reload_host() -> None:
    """只重新加载 agent.host；goal 和 workflow 保持原模块实例。"""
    sys.modules.pop("agent_harness.host", None)


@pytest.fixture(autouse=True)
def isolated_session_directory(tmp_path, monkeypatch):
    from agent_harness.runtime import session_store

    directory = tmp_path / "session-store"
    monkeypatch.setattr(session_store, "sessions_dir", lambda: directory)
    return directory


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    """在 tmp_path 下重新加载 agent.host，使状态目录跟随测试目录。"""
    contexts = ExitStack()

    def load(*, enable_memory=False):
        monkeypatch.chdir(tmp_path)
        _reload_host()
        import agent_harness.host as host

        import agent_harness.core as core
        import agent_harness.agents.subagent as subagent_runtime
        import agent_harness.workspace.tasks as tasks
        import agent_harness.tools.mcp_tools as mcp_tools
        import agent_harness.runtime.scheduler as scheduler
        from agent_harness.runtime.lead_context import LeadContext, lead_context_scope
        # 路径和运行时状态位于模块全局变量中；新会话不能继承上个测试的
        # 启动目录、子 agent、计划审批、任务分配或 MCP 客户端。
        core.WORKDIR = tmp_path
        monkeypatch.setattr(core, "MEMORY_ENABLED", enable_memory)
        subagent_runtime.active_subagents.clear()
        subagent_runtime.PROTOCOL.clear()
        tasks.assignments.clear()
        tasks.assignment_versions.clear()
        contexts.enter_context(lead_context_scope(LeadContext(tasks.task_lock)))
        mcp_tools.mcp_clients.clear()
        scheduler.scheduled_jobs.clear()
        scheduler.cron_queue.clear()
        scheduler.background_tasks.clear()
        scheduler.background_results.clear()
        host.initialize_runtime()
        # 记忆提取会调用模型，测试中由用例显式控制。
        if not enable_memory:
            monkeypatch.setattr(host, "remember_after_turn", lambda messages: None)
        return host

    yield load
    contexts.close()


@pytest.fixture
def workflow_module():
    from agent_harness.workflows import workflow_runtime as workflow

    return workflow


@pytest.fixture
def runtime_factory(host_factory, monkeypatch):
    """通过真实共享驱动启动离线运行时，测试结束统一关闭并核对收尾。"""
    import agent_harness.core as core
    import agent_harness.runtime.scheduler as scheduler
    import agent_harness.agents.subagent as subagent_runtime
    from agent_harness.runtime import interactive_runtime
    from agent_harness.runtime.lead_context import lead_context_scope
    from support.fakes import fake_stream_for, response
    from support.helpers import until

    host = host_factory(enable_memory=True)
    remember = host.remember_after_turn
    monkeypatch.setattr(interactive_runtime, "host", host)
    monkeypatch.setattr(core, "ASYNC_EVENT_POLL_SECONDS", 0.01)
    runtimes = []
    contexts = ExitStack()

    def create(responses=None, client=None, enable_memory=False, stream=None):
        monkeypatch.setattr(host, "remember_after_turn", remember if enable_memory else lambda messages: None)
        if client is None:
            values = iter(responses or [response()])
            client = lambda **kwargs: next(values)
        messages = SimpleNamespace(create=client)
        messages.stream = stream or fake_stream_for(lambda **kwargs: messages.create(**kwargs))
        monkeypatch.setattr(core, "client", SimpleNamespace(messages=messages))
        runtime = interactive_runtime.InteractiveRuntime()
        contexts.enter_context(lead_context_scope(runtime.lead_context))
        runtimes.append(runtime)
        runtime.start()
        until(runtime, "state_snapshot")
        return runtime, host

    yield create
    for runtime in runtimes:
        runtime.close()
        runtime._thread.join(timeout=5)
        assert not runtime._thread.is_alive()
    assert scheduler._runtime_thread is None
    assert not subagent_runtime.active_subagents
    contexts.close()


@pytest.fixture
def approval_hook():
    from agent_harness.runtime.interaction import ExecutionContext, InteractionChannel, execution_context

    def check(hook, block, decision):
        channel = InteractionChannel()
        context = ExecutionContext(channel, "session", 1, "turn", origin="frontend")
        results = []
        def execute():
            with execution_context(context):
                results.append(hook(block))
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            request = channel.next_approval(timeout=2)
            assert request is not None
            assert channel.respond(request.request_id, request.generation, decision)
            worker.join(timeout=2)
        finally:
            channel.close()
            worker.join(timeout=2)
        assert not worker.is_alive()
        return results[0]
    return check
