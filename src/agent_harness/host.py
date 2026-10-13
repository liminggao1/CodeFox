"""集成入口：agent 主循环、工作流和目标的 Stop 边界。

本模块负责连接各功能模块。模型可用的能力分别位于 core（配置）、tasks、
worktrees、task_worktrees、skills、prompts、file_tools、subagent、hooks、
compaction、recovery、scheduler、mcp_tools 和 registrations；调用方直接导入所属模块。
从 cli.py 启动。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

from . import core
from .workspace import tasks
from .workspace import worktrees
from .workspace import task_worktrees
from .context import skills
from .tools import file_tools
from .providers.llm_client import StreamStopped, stream_message
from .agents import subagent
from .agents import recovery
from .runtime import scheduler
from .tools import mcp_tools
from . import registrations
from .tools import tool_registry
from .context import memory_runtime
from . import runtime_context
from .agents import goal as goal_module
from .agents.goal_evaluator import PromptGoalEvaluator
from .agents.turn_engine import PreparedRequest, TurnCallbacks, TurnEngine, TurnRequest
from .workflows import workflow_runtime
from .workflows import workflow_demo
from .runtime import interaction, permissions

def assemble_tool_pool() -> tuple[list[dict], dict]:
    """从同一个注册器快照取得模型工具描述和处理器映射。
    返回：(tools, handlers)，分别为新列表和新字典；装配或名称冲突异常继续抛出。
    """
    snapshot = tool_registry.REGISTRY.snapshot()
    return list(snapshot.tools), dict(snapshot.handlers)

GOAL = None
_initialized = False
_initialization_lock = threading.Lock()


def _create_goal(evaluator_model: str | None = None):
    """Prepare a controller without publishing application state or requesting a model."""
    import os
    model = (evaluator_model
             or os.getenv("GOAL_EVALUATOR_MODEL_ID")
             or os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL")
             or core.MODEL)
    evaluator = PromptGoalEvaluator(client=core.client, model=model)
    block_cap = int(os.getenv("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP",
                              str(goal_module.DEFAULT_STOP_HOOK_BLOCK_CAP)))
    return goal_module.GoalController(evaluator=evaluator, block_cap=block_cap)


def configure_goal(evaluator_model: str | None = None):
    """Explicitly replace the goal; invalid configuration leaves the old goal intact."""
    global GOAL
    GOAL = _create_goal(evaluator_model)
    return GOAL


def initialize_runtime() -> None:
    """Configure the application once, without choosing a runner or starting services.

    Prepare the goal before publishing workflow/registry configuration. Failures
    remain retryable; preserve explicitly configured goals and injected runners.
    """
    global GOAL, _initialized
    with _initialization_lock:
        if _initialized:
            return
        permissions.SETTINGS.load()
        goal = GOAL if GOAL is not None else _create_goal()
        workflow_demo.register_workflows()
        tool_registry.REGISTRY.configure(
            registrations.builtin_definitions,
            mcp_tools.discover_tools,
            lambda: [tool_registry.ToolDefinition(
                workflow_runtime.WORKFLOW_TOOL, workflow_runtime.run_workflow_sync, "Workflow")],
        )
        GOAL = goal
        _initialized = True


def remember_after_turn(messages: list) -> None:
    """从已完成的对话中提取并整理长期记忆。

    Args:
        messages: 已完成本轮处理的完整对话历史。

    Returns:
        ``None``。
    """
    # 未开启长期记忆时不产生任何记忆操作事件，界面不出现“记忆检查/记忆整理”行。
    if not memory_runtime.memory_enabled() or interaction.stopped():
        return
    with interaction.observe_operation("memory", "记忆检查"):
        extracted = memory_runtime.extract_memories(messages)
    if extracted and not interaction.stopped():
        with interaction.observe_operation("memory", "记忆整理"):
            memory_runtime.consolidate_memories()


# -- Agent 主循环 --

agent_lock = threading.Lock()


def build_user_content(results: list[dict]) -> list[dict]:
    """组合工具结果和后台任务通知。

    Args:
        results: 当前批次的工具结果列表。

    Returns:
        可直接放入下一条 ``user`` 消息的新内容列表。
    """
    # 工具结果和已完成的后台通知都作为 user 侧内容返回模型，沿用 tool_result 反馈流程。
    content = list(results)
    try:
        notes = scheduler.collect_background_results()
    except OSError as exc:
        core.terminal_print(f"[background error] Could not save results: {exc}")
        notes = []
    for note in notes:
        content.append({"type": "text", "text": note})
    return content


def call_llm(prepared_request: PreparedRequest, state: recovery.RecoveryState):
    """调用模型并返回响应。

    Args:
        prepared_request: 已完成上下文准备和计量的 API 请求快照。
        state: 本轮模型重试和恢复状态。

    Returns:
        完整响应、调用 ID 和生命周期 ID；无交互上下文时后两个为 None。
    """
    def request():
        if interaction.stopped():
            raise StreamStopped("Stopped before requesting model")
        execution = interaction.current_context()
        call_id = uuid.uuid4().hex if execution is not None else None
        operation = interaction.operation_status("model", "请求模型", "running", model_call_id=call_id) if call_id else None
        sequence = 0
        def on_text(index, text):
            nonlocal sequence
            if text and not interaction.stopped():
                sequence += 1
                interaction.publish("assistant_text_delta", model_call_id=call_id,
                                    block_index=index, sequence=sequence, delta=text)
        kwargs = dict(
            model=state.current_model,
            system=prepared_request.system,
            messages=prepared_request.messages,
            tools=prepared_request.tools,
            max_tokens=state.max_tokens)
        try:
            response = (stream_message(core.client, on_text=on_text, stopped=interaction.stopped, **kwargs)
                        if execution is not None and execution.source == "lead"
                        else core.client.messages.create(**kwargs))
        except Exception as exc:
            if call_id:
                interaction.operation_status("model", "请求模型", "stopped" if isinstance(exc, StreamStopped) else "failed",
                                             operation_id=operation, model_call_id=call_id,
                                             reason=f"{type(exc).__name__}: {exc}")
            raise exc
        return response, call_id, operation
    return recovery.with_retry(request, state, stopped=interaction.stopped)


def evaluate_goal(messages: list):
    """评估当前会话目标是否满足，并转换为本轮停止决定。
    Args:
        messages: 当前完整对话历史，供目标评估器判断完成条件。
    Returns:
        ``StopDecision``，其 action 表示继续、完成、延迟或错误等结果。
        无活动目标或外层执行异常时返回 None；评估器内部失败通常返回 error 决定。
    """
    # 没有创建目标控制器，或控制器当前没有活动目标时，不需要评估。
    if GOAL is None or GOAL.active is None:
        # 返回 None，让 agent_loop 按普通回答完成路径继续处理。
        return None
    operation = interaction.operation_status("goal", "目标检查", "running")
    try:
        # evaluate_after_turn 是异步函数；这里在同步的 agent_loop 中运行它。
        decision = asyncio.run(GOAL.evaluate_after_turn(
            # 传入完整对话，让独立评估器根据对话证据判断目标是否完成。
            messages, background_running=scheduler.has_pending_background()))
        interaction.operation_status("goal", "目标检查", "failed" if decision.action == "error" else "completed",
                                     operation_id=operation, reason=decision.reason)
        return decision
    except Exception as exc:
        interaction.operation_status("goal", "目标检查", "failed", operation_id=operation,
                                     reason=f"{type(exc).__name__}: {exc}")
        # 评估器异常不应中断主循环，先把异常类型和信息打印出来。
        core.terminal_print(f"  \033[31m[goal] evaluator error: "
              f"{type(exc).__name__}: {exc}\033[0m")
        # 返回 None，使本轮按未设置目标的方式结束。
        return None


# Agent 主循环入口：实际执行顺序由 TurnEngine 拥有，host 只负责装配宿主回调。
def agent_loop(history: list, active_request: str, save_context=None, on_usage=None, *, prompt_state=None):
    """兼容入口：装配宿主回调并执行一轮 TurnEngine。

    保留历史调用方使用的参数和返回值；具体的上下文准备、模型重试/续写、工具
    批次、Goal/memory 收尾和停止终态由 ``TurnEngine`` 负责。
    """
    callbacks = TurnCallbacks(
        assemble_tool_pool=assemble_tool_pool,
        build_context=runtime_context.build_context,
        call_llm=call_llm,
        build_user_content=build_user_content,
        evaluate_goal=evaluate_goal,
        remember_after_turn=remember_after_turn,
        goal_condition=lambda: GOAL.active.condition if GOAL is not None and GOAL.active else "",
    )
    return TurnEngine(callbacks).run(TurnRequest(
        history=history,
        active_request=active_request,
        save_context=save_context,
        on_usage=on_usage,
        prompt_state=prompt_state,
    ))
