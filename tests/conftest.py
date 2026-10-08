"""测试共用配置：项目导入路径、离线环境默认值和 host 工厂。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# 即使没有本地 .env，也保证导入过程离线且确定。
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("LLM_API", "responses")


def _reload_host() -> None:
    """只重新加载 agent.host；goal 和 workflow 保持原模块实例。"""
    sys.modules.pop("agent_harness.host", None)


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    """在 tmp_path 下重新加载 agent.host，使状态目录跟随测试目录。"""

    def load():
        monkeypatch.chdir(tmp_path)
        _reload_host()
        import agent_harness.host as host

        # 路径和运行时状态位于模块全局变量中；新会话不能继承上个测试的
        # 启动目录、队友、计划审批、任务分配或 MCP 客户端。
        host.core.WORKDIR = tmp_path
        host.teams.active_teammates.clear()
        host.teams.plan_gates.clear()
        host.teams.plan_gate_versions.clear()
        host.teams.plan_request_ids.clear()
        host.teams.pending_requests.clear()
        host.tasks.teammate_assignments.clear()
        host.tasks.assignment_versions.clear()
        host.tasks.CURRENT_TODOS.clear()
        host.mcp_clients.clear()
        host.scheduler.scheduled_jobs.clear()
        host.scheduler.cron_queue.clear()
        host.scheduler.background_tasks.clear()
        host.scheduler.background_results.clear()
        # 记忆提取会调用模型，测试中由用例显式控制。
        monkeypatch.setattr(host, "remember_after_turn", lambda messages: None)
        return host

    return load


@pytest.fixture
def workflow_module():
    from agent_harness import workflow_runtime as workflow

    return workflow
