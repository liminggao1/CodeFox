"""真实 API 测试 fixture；目录由根 conftest 的 LIVE_API 开关控制。"""

from __future__ import annotations

import pytest


@pytest.fixture
def live_runtime(tmp_path, monkeypatch):
    """启动使用项目真实配置的共享交互运行时，并隔离会话文件。"""
    import agent_harness.core as core
    import agent_harness.host as host
    from agent_harness.runtime.interactive_runtime import InteractiveRuntime
    from agent_harness.runtime.session_store import SessionStore
    from support.live import drain_runtime_closed

    monkeypatch.chdir(tmp_path)
    core.WORKDIR = tmp_path
    core.MEMORY_ENABLED = False
    host.GOAL = None
    host._initialized = False

    store = SessionStore(tmp_path)
    store.directory = tmp_path / "sessions"
    runtime = InteractiveRuntime(store=store)
    runtime.start()
    try:
        yield runtime, store
    finally:
        if runtime._thread is not None and runtime._thread.is_alive():
            assert drain_runtime_closed(runtime), "live runtime did not close cleanly"

