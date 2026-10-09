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
from pathlib import Path

from . import core
from .workspace import tasks
from .workspace import worktrees
from .workspace import task_worktrees
from .context import skills
from .context import prompts
from .tools import file_tools
from .runtime import hooks
from .agents import subagent
from .providers import message_content
from .context import compaction
from .agents import recovery
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
from .runtime.session_store import json_value

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
    active_subagents, consume_lead_inbox, format_subagent_events, plan_gates,
    plan_request_ids, pending_requests, ProtocolState, new_request_id,
    match_response, is_valid_agent_name, SubagentRecord,
    current_work_identity, apply_plan_response,
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
    if not interaction.stopped() and memory_runtime.extract_memories(messages) and not interaction.stopped():
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
        模型客户端返回的响应对象。
    """
    system = prompts.assemble_system_prompt(context.prompt_values())
    return recovery.with_retry(
        lambda: core.client.messages.create(
            model=state.current_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=state.max_tokens),
        state)


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
    try:
        # evaluate_after_turn 是异步函数；这里在同步的 agent_loop 中运行它。
        return asyncio.run(GOAL.evaluate_after_turn(
            # 传入完整对话，让独立评估器根据对话证据判断目标是否完成。
            messages, background_running=scheduler.has_pending_background()))
    except Exception as exc:
        # 评估器异常不应中断主循环，先把异常类型和信息打印出来。
        core.terminal_print(f"  \033[31m[goal] evaluator error: "
              f"{type(exc).__name__}: {exc}\033[0m")
        # 返回 None，使本轮按未设置目标的方式结束。
        return None


# Agent 主循环：准备上下文、调用模型、执行工具并回填结果，直到本轮结束。
def agent_loop(messages: list, active_request: str, save_context=None, on_usage=None):
    """执行 Agent 主循环，直到模型完成回答或本轮无法继续。
    Args:
        messages: 当前对话历史，会被原地追加助手响应和工具结果。
        active_request: 当前用户请求，供历史压缩和任务续做使用。
    Returns:
        TurnOutcome; history and tool side effects remain with their owners.
    """
    global rounds_since_todo
    result_states = {}

    def checkpoint():
        if save_context:
            save_context()

    def append(message):
        messages.append(message)
        if message.get("role") == "assistant":
            for block in json_value(message["content"]):
                if block.get("type") == "text":
                    interaction.publish("assistant_message", text=block["text"])
                elif block.get("type") == "tool_use":
                    interaction.publish("tool_call", **block)
        elif isinstance(message.get("content"), list):
            for block in json_value(message["content"]):
                if block.get("type") == "tool_result":
                    interaction.publish("tool_result", **block,
                                        status=result_states.pop(block["tool_use_id"],
                                                                 "failed" if block.get("is_error") else "returned"))
        checkpoint()

    def stopped_result():
        scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
        tasks.release_completed_assignment("agent")
        return interaction.TurnOutcome("stopped", "Stop requested")

    # 保存本轮 API 重试、模型降级和上下文恢复状态。
    state = recovery.RecoveryState()
    # 管理本轮手动压缩的工具响应和执行时机。
    manual_compaction = compaction.ManualCompaction()
    automatic_compaction_enabled = True
    # 暂存已取出、尚未确认交付的定时任务。
    unacknowledged_cron_jobs: list[scheduler.CronJob] = []

    # 每次迭代完成一次模型调用，并处理它返回的文本或工具请求。
    while True:
        if interaction.stopped():
            return stopped_result()
        # 从队列中取出本次到期的定时任务。
        fired = scheduler.consume_cron_queue()
        # 调用模型成功前保留这些任务，以便失败时重新入队。
        unacknowledged_cron_jobs.extend(fired)
        model_call_started = False
        # Preparation and model failures share the same cron cleanup.
        try:
            # 将每个定时任务作为一条用户消息送入对话历史。
            for job in fired:
                # 模型通过用户消息读取定时任务内容。
                append({"role": "user",
                                 "content": f"[Scheduled] {job.prompt}"})
                # 在终端显示截短的注入提示。
                core.terminal_print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
            # 有定时任务时也把它们纳入压缩摘要使用的当前请求。
            if fired:
                # 将本批定时任务合并成多行请求。
                scheduled_requests = "\n".join(
                    f"Run scheduled task: {job.prompt}" for job in fired)
                # 保留原请求，并追加定时任务内容。
                active_request = f"{active_request}\n{scheduled_requests}".strip()
            # 将已完成的后台任务通知加入对话历史。
            inject_background_notifications(messages, save_context)

            # 达到未更新 TODO 的工具轮数阈值时，提醒模型更新。
            if rounds_since_todo >= core.TODO_REMINDER_ROUNDS:
                # 把提醒作为用户消息加入历史。
                append({"role": "user",
                                 "content": "<reminder>Update your todos.</reminder>"})
                # 本次提醒后重新累计调用次数。
                rounds_since_todo = 0

            # 按上下文预算裁剪或压缩历史消息。
            compacted = compaction.prepare_history(
                messages, active_request, enabled=automatic_compaction_enabled)
            if compacted.applied:
                checkpoint()
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
                return stopped_result()

            model_call_started = True
            # 重试策略和模型降级由 call_llm 内部处理。
            response = call_llm(messages, context, tools, state)
        # Restore unacknowledged jobs if preparation or the model call fails.
        except Exception as e:
            # 首次遇到提示过长错误时压缩历史，再重新调用模型。
            if (not interaction.stopped() and model_call_started and recovery.is_prompt_too_long_error(e)
                    and state.claim_reactive_compaction()):
                # 原地替换历史，保留调用方持有的列表引用。
                compacted = compaction.reactive_compact(messages, active_request)
                if compacted.applied:
                    checkpoint()
                if compacted.reason is None:
                    continue
                e = RuntimeError(f"Reactive compaction failed: {compacted.reason}")
            # 调用最终失败时，将未确认的定时任务重新放回队列。
            scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
            # 将错误写入历史，供外层向用户展示。
            append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            # 释放已完成任务的 agent 分配记录。
            tasks.release_completed_assignment("agent")
            # 结束本轮交互，不继续执行工具。
            return interaction.TurnOutcome("failed", f"{type(e).__name__}: {e}")

        # 模型已成功响应，确认本批定时任务已交付。
        try:
            # 将已消费的任务从持久化记录中确认移除。
            scheduler.acknowledge_cron_jobs(unacknowledged_cron_jobs)
        # 确认写入失败不阻止处理模型响应。
        except OSError as exc:
            # 在终端报告确认失败。
            core.terminal_print(f"[cron error] Delivery confirmation failed: {exc}")
        # 清空本轮待确认清单。
        unacknowledged_cron_jobs.clear()

        usage = getattr(response, "usage", None)
        if usage is not None:
            usage = json_value(usage)
            if on_usage is not None:
                on_usage(usage)
            interaction.publish("lead_usage", usage=usage)

        action = state.accept_response(response.stop_reason)
        if interaction.stopped() and action in {"retry", "continue", "stop"}:
            # Truncated tool requests remain unsafe to execute or persist.
            text = message_content.extract_text(response.content)
            if text:
                append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            return stopped_result()
        if action == "retry":
            core.terminal_print(f"  \033[33m[max_tokens] retry with {state.max_tokens}\033[0m")
            continue
        if action in {"continue", "stop"}:
            # Preserve partial text only after the upgraded call has returned.
            append({"role": "assistant", "content": response.content})
            if action == "continue":
                # 添加续写提示，作为下一次模型调用的用户消息。
                append({"role": "user", "content": core.CONTINUATION_PROMPT})
                # 返回循环顶部继续调用模型。
                continue
            # 续写机会耗尽时释放已完成的分配记录。
            tasks.release_completed_assignment("agent")
            # 停止本轮交互。
            return interaction.TurnOutcome("truncated", "Output continuation limit reached")

        # 保存模型响应，供工具结果和后续调用引用。
        append({"role": "assistant", "content": response.content})
        # 没有工具请求时，模型已完成当前一轮回答。
        if not message_content.has_tool_use(response.content):
            if interaction.stopped():
                return stopped_result()
            # 通知 Stop 钩子本轮模型输出已完成。
            hooks.trigger_hooks("Stop", messages)
            if interaction.stopped():
                return stopped_result()
            # 检查当前目标是否允许结束。
            decision = evaluate_goal(messages)
            if decision is not None:
                checkpoint()
            if interaction.stopped():
                return stopped_result()
            # 目标未达成时，反馈缺失条件并继续调用模型。
            if decision is not None and decision.action == "block":
                # 读取仍在生效的目标条件。
                condition = GOAL.active.condition if GOAL.active else ""
                # 在终端显示要求继续的原因。
                core.terminal_print(f"  \033[35m[goal] continue: "
                      f"{decision.reason[:80]}\033[0m")
                # 将目标条件和评估结果写回历史。
                if interaction.stopped():
                    return stopped_result()
                append({"role": "user", "content": (
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
                core.terminal_print(f"  \033[35m[goal] {decision.action}: "
                      f"{decision.reason}\033[0m")
            # 从已完成的对话中提取并整理长期记忆。
            remember_after_turn(messages)
            if interaction.stopped():
                return stopped_result()
            # 释放已完成任务的 agent 分配记录。
            tasks.release_completed_assignment("agent")
            # 本轮回答结束。
            return interaction.TurnOutcome("completed")

        # 本次响应包含工具调用，准备收集工具执行结果。
        results = []
        # 按模型返回顺序处理每个内容块。
        for block in response.content:
            # 非工具块保留在助手消息中，无需执行。
            if block.type != "tool_use":
                # 跳过文本等非工具内容。
                continue
            if interaction.stopped():
                result_states[block.id] = "stopped"
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Interrupted before execution", "is_error": True})
                continue
            # 在终端显示当前工具名称。
            if interaction.current_context() is None:
                print(f"\033[36m> {block.name}\033[0m")

            # 将手动压缩请求交给压缩模块处理。
            compact_result = manual_compaction.handle_tool_use(block)
            # compact 不经过普通工具钩子和执行器。
            if compact_result is not None:
                # 保留与模型工具调用对应的结果。
                results.append(compact_result)
                # 其他工具继续按原有路径执行。
                continue

            # 执行工具前通知钩子，允许它阻止本次调用。
            blocked = hooks.trigger_hooks("PreToolUse", block)
            if interaction.stopped():
                result_states[block.id] = "stopped"
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Interrupted before execution", "is_error": True})
                continue
            # 钩子拒绝调用时，将原因作为工具结果返回。
            if blocked:
                result_states[block.id] = "denied"
                # 使用原工具调用 ID 与结果配对。
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                # 不再执行被阻止的工具。
                continue

            # 可后台执行的工具不阻塞当前模型轮次。
            if scheduler.should_run_background(block.name, block.input):
                # 启动后台任务；失败时也要返回工具结果。
                try:
                    # 将工具调用交给后台任务调度器。
                    bg_id = scheduler.start_background_task(block, handlers)
                    result_states[block.id] = "started"
                    # 告知模型结果稍后通过任务通知送达。
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                # 将启动异常转换成模型可读的结果。
                except Exception as exc:
                    result_states[block.id] = "failed"
                    # 保留异常类型和信息，便于模型判断失败原因。
                    output = (f"Error: Failed to start background task: "
                              f"{type(exc).__name__}: {exc}")
                # 为本次后台工具调用登记返回内容。
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                # 后台工具不再进入同步执行路径。
                continue

            # 根据工具名查找同步处理函数。
            handler = handlers.get(block.name)
            # 执行工具，并把未知工具或执行异常转成结果文本。
            output = file_tools.call_tool_handler(
                handler, block.input, block.name,
                on_status=lambda status: result_states.update({block.id: status}))
            # 执行完成后通知 PostToolUse 钩子。
            hooks.trigger_hooks("PostToolUse", block, output)
            # 在终端显示截短的执行结果。
            if interaction.current_context() is None:
                print(str(output)[:300])

            # 写入 TODO 后重新开始计算提醒间隔。
            if block.name == "todo_write":
                # 清零已累计的非 TODO 工具调用次数。
                rounds_since_todo = 0
            # 其他工具调用会增加 TODO 提醒计数。
            else:
                # 记录又完成了一次非 TODO 工具调用。
                rounds_since_todo += 1

            # 将同步工具输出与原调用 ID 配对。
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})

        # 把所有工具结果及已完成的后台通知写入历史。
        append({"role": "user", "content": build_user_content(results)})
        if interaction.stopped():
            return stopped_result()
        # 如本轮请求了手动压缩，在工具结果回填后生成摘要。
        if manual_compaction.apply_after_results(messages, active_request) is not None:
            checkpoint()
