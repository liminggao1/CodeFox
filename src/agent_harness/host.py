"""集成入口：agent 主循环、工作流和目标的 Stop 边界。

本模块负责连接各功能模块。模型可用的能力分别位于 core（配置）、tasks、
worktrees、task_worktrees、skills、prompts、file_tools、subagent、hooks、
subagent、compaction、recovery、scheduler、mcp_tools 和 registrations。
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
from .context import prompts
from .tools import file_tools
from .runtime import hooks
from .providers.llm_client import StreamStopped, stream_message
from .agents import subagent
from .providers import message_content
from .context import compaction
from .agents import recovery
from .agents.lead_turn import LeadTurn
from .agents.lead_tools import execute_tool_batch
from .runtime import scheduler
from .tools import mcp_tools
from . import registrations
from .tools import tool_registry
from .context import memory_runtime
from . import runtime_context
from .agents import goal as goal_module
from .workflows import workflow_runtime
from .workflows import workflow_demo
from .runtime import interaction

# Re-export names used by the CLI and source-level callers.
from .core import (  # noqa: F401
    CONSOLE, PROMPT, READLINE_PROMPT,
    terminal_print,
)
from .workspace.tasks import (  # noqa: F401
    CURRENT_TODOS, Task, assignment_cwd, assignment_versions, claim_task,
    complete_task, create_task, get_task_json, list_tasks, load_task,
    release_completed_assignment, release_assignment, save_task,
    task_lock, task_worktree_cwd, assignments, update_task,
)
from .workspace.worktrees import (  # noqa: F401
    run_git, _registered_worktrees, _registered_worktree,
)
from .workspace.task_worktrees import create_worktree, remove_worktree  # noqa: F401
from .context.skills import list_skills, load_skill, SKILL_REGISTRY  # noqa: F401
from .context.prompts import PROMPT_SECTIONS, ENVIRONMENT_PROMPT  # noqa: F401
from .tools.file_tools import (  # noqa: F401
    call_tool_handler, run_agent_bash, run_agent_edit, run_agent_glob,
    run_agent_read, run_agent_write, run_bash, run_edit, run_glob, run_read,
    run_todo_write, run_write, safe_path,
)
from .agents.subagent import (  # noqa: F401
    BUS, MessageBus, RESERVED_SUBAGENT_NAMES,
    active_subagents, consume_lead_inbox, format_subagent_events,
    is_valid_agent_name, SubagentRecord, apply_plan_response,
    apply_shutdown_request, send_message, submit_plan,
    run_request_plan, run_request_shutdown,
    run_review_plan, subagent_lock,
)
from .runtime.hooks import (  # noqa: F401
    register_hook, trigger_hooks, permission_hook, log_hook, large_output_hook,
    user_prompt_hook, stop_hook, HOOKS, DENY_LIST,
)
from .agents.subagent import spawn_subagent  # noqa: F401
from .context.compaction import (  # noqa: F401
    block_type, compact_history, estimate_size, micro_compact, snip_compact,
    tool_result_budget, fit_tool_results, reactive_compact,
)
from .runtime.scheduler import (  # noqa: F401
    collect_background_results, has_pending_background, start_runtime_services,
    should_run_background, start_background_task, consume_cron_queue,
    acknowledge_cron_jobs, restore_cron_jobs, cron_lock, cron_queue, CronJob,
    schedule_job, cancel_job, run_schedule_cron, run_list_crons, run_cancel_cron,
)
from .tools.mcp_tools import (  # noqa: F401
    connect_mcp, mcp_clients, MCPClient, MOCK_SERVERS,
    normalize_mcp_name, MCP_HOST_POLICY,
)
from .registrations import (  # noqa: F401
    BUILTIN_TOOLS, BUILTIN_HANDLERS, run_create_task, run_update_task,
    run_list_tasks, run_get_task, run_claim_task, run_complete_task,
    run_spawn_subagent, run_list_subagents, run_send_message,
    run_connect_mcp, run_create_worktree, run_remove_worktree,
)
from .agents.recovery import RecoveryState  # noqa: F401

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
    evaluator = goal_module.PromptGoalEvaluator(client=core.client, model=model)
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
    if interaction.stopped():
        return
    with interaction.observe_operation("memory", "记忆检查"):
        extracted = memory_runtime.extract_memories(messages)
    if extracted and not interaction.stopped():
        with interaction.observe_operation("memory", "记忆整理"):
            memory_runtime.consolidate_memories()


# -- Agent 主循环 --

rounds_since_todo = 0
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


def inject_background_notifications(messages: list, save_context=None):
    """把已完成的后台任务通知追加到对话历史。

    Args:
        messages: 当前对话历史，会在有通知时原地追加消息。

    Returns:
        ``None``。
    """
    try:
        notes = scheduler.collect_background_results()
    except OSError as exc:
        core.terminal_print(f"[background error] Could not save results: {exc}")
        return
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})
        if save_context:
            save_context()


def call_llm(messages: list, context: runtime_context.RuntimeContext, tools: list,
             state: recovery.RecoveryState):
    """调用模型并返回响应。

    Args:
        messages: 当前对话历史。
        context: 用于生成系统提示词的运行时上下文。
        tools: 本轮可用的工具描述列表。
        state: 本轮模型重试和恢复状态。

    Returns:
        完整响应、调用 ID 和生命周期 ID；无交互上下文时后两个为 None。
    """
    system = prompts.assemble_system_prompt(context.prompt_values())
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
            system=system,
            messages=messages,
            tools=tools,
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


def _track_tool_completion(name):
    """跨回合保留 TODO 提醒计数，批次中途失败也保留已完成工具的计数。"""
    global rounds_since_todo
    # 写入 TODO 后重新开始计算提醒间隔。
    if name == "todo_write":
        # 清零已累计的非 TODO 工具调用次数。
        rounds_since_todo = 0
    # 其他工具调用会增加 TODO 提醒计数。
    else:
        # 记录又完成了一次非 TODO 工具调用。
        rounds_since_todo += 1


# Agent 主循环：准备上下文、调用模型、执行工具并回填结果，直到本轮结束。
def agent_loop(messages: list, active_request: str, save_context=None, on_usage=None):
    """执行 Agent 主循环，直到模型完成回答或本轮无法继续。
    Args:
        messages: 当前对话历史，会被原地追加助手响应和工具结果。
        active_request: 当前用户请求，供历史压缩和任务续做使用。
        save_context: 运行时提供的检查点回调，负责保存会话。
        on_usage: 接收 Lead 主模型用量的回调。
    Returns:
        TurnOutcome；历史和工具副作用保留在各自所属模块。
    LeadTurn 负责本轮记录与收尾，工具批次复用 hooks；本循环保留
    上下文准备、模型重试/续写、goal 判定和工具迭代的显式顺序。
    """
    # 全局变量，记录距离上次待办任务 todo 的迭代轮数，用于任务续做/压缩判断
    global rounds_since_todo
    # 创建本轮回合对象 LeadTurn：管理本轮所有记录、状态、会话保存、token用量回调
    turn = LeadTurn(messages, save_context, on_usage)
    # 实例化恢复状态对象：保存本轮API重试、模型降级、上下文回滚恢复相关状态
    state = recovery.RecoveryState()
    # 手动压缩管理器，管控手动触发的工具返回内容压缩以及压缩执行时机
    manual_compaction = compaction.ManualCompaction()
    # 开启自动上下文压缩开关，当上下文过长时自动压缩对话历史
    automatic_compaction_enabled = True

    # -- 准备上下文并请求完整模型响应 --
    # 每次循环迭代：调用一次模型，处理模型返回的文本回答 或者 工具调用请求
    while True:
        # 检查外部停止信号（用户主动终止、运行时关闭），收到信号就结束本轮
        if interaction.stopped():
            return turn.finish("stopped")
        # 标记模型调用已经开始，用于异常捕获、重试逻辑判断
        model_call_started = False
        # 准备阶段与模型调用失败共用同一套 cron 收尾。
        try:
            active_request = turn.inject_scheduled(active_request)
            # 将已完成的后台任务通知加入对话历史。
            inject_background_notifications(messages, save_context)
            # 达到未更新 TODO 的工具轮数阈值时，提醒模型更新。
            if rounds_since_todo >= core.TODO_REMINDER_ROUNDS:
                # 把提醒作为用户消息加入历史。
                turn.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
                # 本次提醒后重新累计调用次数。
                rounds_since_todo = 0

            # 按上下文预算裁剪或压缩历史消息。
            compacted = compaction.prepare_history(
                messages, active_request, enabled=automatic_compaction_enabled)
            if compacted.applied:
                turn.checkpoint()
            if compacted.reason:
                automatic_compaction_enabled = False
                core.terminal_print(f"[compact error] Automatic compaction disabled for this turn: {compacted.reason}")
            # 根据最新消息刷新记忆、MCP 和子 agent上下文。
            context = runtime_context.build_context(messages)
            # 每轮重新读取工具池，包含运行中新增的 MCP 工具。
            tools, handlers = assemble_tool_pool()
            # 手动压缩完成后不再向模型提供 compact 工具。
            tools = manual_compaction.available_tools(tools)
            if interaction.stopped():
                return turn.finish("stopped")

            model_call_started = True
            # 重试策略和模型降级由 call_llm 内部处理。
            turn.begin_model_call()
            response, call_id, operation_id = call_llm(messages, context, tools, state)
        # 准备阶段或模型调用失败时，归还尚未确认的定时任务。
        except Exception as exc:
            if isinstance(exc, StreamStopped):
                return turn.finish("stopped")
            # 准备阶段错误不消耗模型上下文恢复机会。
            # 首次遇到提示过长错误时压缩历史，再重新调用模型。
            if (not interaction.stopped() and model_call_started
                    and recovery.is_prompt_too_long_error(exc)
                    and state.claim_reactive_compaction()):
                # 原地替换历史，保留调用方持有的列表引用。
                compacted = compaction.reactive_compact(messages, active_request)
                if compacted.applied:
                    turn.checkpoint()
                if compacted.reason is None:
                    continue
                exc = RuntimeError(f"Reactive compaction failed: {compacted.reason}")
            # 结束本轮交互，不继续执行工具。
            return turn.finish("failed", f"{type(exc).__name__}: {exc}")

        # 模型已成功响应，确认本批定时任务已交付。
        turn.acknowledge_scheduled()
        action = turn.accept_response(response, call_id, operation_id, state)

        # -- 接受工具请求前，先处理输出截断的重试与续写 --
        if interaction.stopped() and action in {"retry", "continue", "stop"}:
            # 截断的工具请求不能执行或保存，只保留可用文本。
            text = message_content.extract_text(response.content)
            if text:
                turn.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            return turn.finish("stopped")
        if action == "retry":
            core.terminal_print(f"  \033[33m[max_tokens] retry with {state.max_tokens}\033[0m")
            continue
        if action in {"continue", "stop"}:
            # 输出额度升级的请求返回后，才保留可续写的部分文本。
            turn.append({"role": "assistant", "content": response.content})
            if action == "continue":
                # 添加续写提示，作为下一次模型调用的用户消息。
                turn.append({"role": "user", "content": core.CONTINUATION_PROMPT})
                # 返回循环顶部继续调用模型。
                continue
            # 续写机会耗尽时释放已完成的分配记录。
            # 停止本轮交互。
            return turn.finish("truncated", "Output continuation limit reached")

        # 保存模型响应，供工具结果和后续调用引用。
        turn.append({"role": "assistant", "content": response.content})

        # -- 判断目标是否允许本轮回答结束 --
        # 没有工具请求时，模型已完成当前一轮回答。
        if not message_content.has_tool_use(response.content):
            if interaction.stopped():
                return turn.finish("stopped")
            # 通知 Stop 钩子本轮模型输出已完成。
            hooks.trigger_hooks("Stop", messages)
            if interaction.stopped():
                return turn.finish("stopped")
            # 检查当前目标是否允许结束。
            decision = evaluate_goal(messages)
            if decision is not None:
                turn.checkpoint()
            if interaction.stopped():
                return turn.finish("stopped")
            # 目标未达成时，反馈缺失条件并继续调用模型。
            if decision is not None and decision.action == "block":
                # 读取仍在生效的目标条件。
                condition = GOAL.active.condition if GOAL.active else ""
                # 在终端显示要求继续的原因。
                core.terminal_print(f"  \033[35m[goal] continue: {decision.reason[:80]}\033[0m")
                # 将目标条件和评估结果写回历史。
                if interaction.stopped():
                    return turn.finish("stopped")
                turn.append({"role": "user", "content": (
                    "[Goal still active]\n"
                    f"Condition: {condition}\n"
                    f"Evaluator: {decision.reason}\n"
                    "Continue working and surface the missing evidence."
                )})
                # 再次调用模型寻找缺失证据。
                continue
            # 对非 allow 的其他评估结果仅输出提示。
            if decision is not None and decision.action != "allow":
                # 在终端显示评估结果和原因。
                core.terminal_print(f"  \033[35m[goal] {decision.action}: {decision.reason}\033[0m")
            # 从已完成的对话中提取并整理长期记忆。
            remember_after_turn(messages)
            if interaction.stopped():
                return turn.finish("stopped")
            # 释放已完成任务的 agent 分配记录。
            # 本轮回答结束。
            return turn.finish("completed")

        # -- 逐项结算工具，再将完整结果批次交给模型 --
        results = execute_tool_batch(
            response.content, handlers, manual_compaction, turn, _track_tool_completion)
        # 把所有工具结果及已完成的后台通知写入历史。
        turn.append({"role": "user", "content": build_user_content(results)})
        if interaction.stopped():
            return turn.finish("stopped")
        # 如本轮请求了手动压缩，在工具结果回填后生成摘要。
        if manual_compaction.apply_after_results(messages, active_request) is not None:
            turn.checkpoint()
