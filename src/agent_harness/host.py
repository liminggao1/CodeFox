"""集成入口：agent 主循环、工作流和目标的 Stop 边界。

本模块负责连接各功能模块。模型可用的能力分别位于 core（配置）、tasks、
worktrees、task_worktrees、skills、prompts、file_tools、teams、hooks、
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
from . import tasks
from . import worktrees
from . import task_worktrees
from . import skills
from . import prompts
from . import file_tools
from . import teams
from . import hooks
from . import subagent
from . import compaction
from . import recovery
from . import scheduler
from . import mcp_tools
from . import registrations
from . import tool_registry
from . import memory_runtime
from . import runtime_context
from . import goal as goal_module
from . import workflow_runtime
from . import workflow_demo

# 重新导出 cli.py 和 flat 构建依赖的名称。
from .core import (  # noqa: F401
    CONSOLE, PROMPT, READLINE_PROMPT,
    terminal_print,
)
from .tasks import (  # noqa: F401
    CURRENT_TODOS, Task, assignment_cwd, assignment_versions, claim_task,
    complete_task, create_task, get_task_json, list_tasks, load_task,
    release_completed_assignment, release_teammate_assignment, save_task,
    task_lock, task_worktree_cwd, teammate_assignments, update_task,
)
from .worktrees import (  # noqa: F401
    run_git, _registered_worktrees, _registered_worktree,
)
from .task_worktrees import create_worktree, remove_worktree  # noqa: F401
from .skills import list_skills, load_skill, SKILL_REGISTRY  # noqa: F401
from .prompts import PROMPT_SECTIONS, ENVIRONMENT_PROMPT  # noqa: F401
from .file_tools import (  # noqa: F401
    call_tool_handler, run_agent_bash, run_agent_edit, run_agent_glob,
    run_agent_read, run_agent_write, run_bash, run_edit, run_glob, run_read,
    run_todo_write, run_write, safe_path,
)
from .teams import (  # noqa: F401
    BUS, MessageBus, RESERVED_TEAMMATE_NAMES,
    active_teammates, consume_lead_inbox, format_team_events, plan_gates,
    plan_request_ids, pending_requests, ProtocolState, new_request_id,
    match_response, is_valid_agent_name, scan_unclaimed_tasks, claim_next_task,
    current_work_identity, apply_plan_response,
    apply_shutdown_request, _teammate_send_message, _teammate_submit_plan,
    spawn_teammate_thread, run_request_plan, run_request_shutdown,
    run_review_plan, team_lock,
)
from .hooks import (  # noqa: F401
    register_hook, trigger_hooks, permission_hook, log_hook, large_output_hook,
    user_prompt_hook, stop_hook, HOOKS, DENY_LIST,
)
from .subagent import spawn_subagent  # noqa: F401
from .compaction import (  # noqa: F401
    block_type, compact_history, estimate_size, micro_compact, snip_compact,
    tool_result_budget, fit_tool_results, reactive_compact,
)
from .scheduler import (  # noqa: F401
    collect_background_results, has_pending_background, start_runtime_services,
    should_run_background, start_background_task, consume_cron_queue,
    acknowledge_cron_jobs, restore_cron_jobs, cron_lock, cron_queue, CronJob,
    schedule_job, cancel_job, run_schedule_cron, run_list_crons, run_cancel_cron,
)
from .mcp_tools import (  # noqa: F401
    connect_mcp, mcp_clients, MCPClient, MOCK_SERVERS,
    normalize_mcp_name, MCP_HOST_POLICY,
)
from .registrations import (  # noqa: F401
    BUILTIN_TOOLS, BUILTIN_HANDLERS, run_create_task, run_update_task,
    run_list_tasks, run_get_task, run_claim_task, run_complete_task,
    run_spawn_teammate, run_list_teammates, run_send_message,
    run_connect_mcp, run_create_worktree,
)
from .recovery import RecoveryState  # noqa: F401

scheduler.restore_cron_jobs([])
try:
    from .scheduler import load_durable_jobs
    load_durable_jobs()
except Exception:
    pass

workflow_demo.register_workflows()
workflow_runtime.RUNNER_FACTORY = (
    lambda: workflow_runtime.AnthropicAgentRunner(core.client, core.MODEL)
)

tool_registry.REGISTRY.configure(
    registrations.builtin_definitions,
    mcp_tools.discover_tools,
    lambda: [tool_registry.ToolDefinition(
        workflow_runtime.WORKFLOW_TOOL, workflow_runtime.run_workflow_sync, "Workflow")],
)


def assemble_tool_pool() -> tuple[list[dict], dict]:
    """Return the model tools and handlers from the single registry snapshot."""
    snapshot = tool_registry.REGISTRY.snapshot()
    return list(snapshot.tools), dict(snapshot.handlers)

GOAL = None


def configure_goal(evaluator_model: str | None = None):
    """创建 Stop 边界使用的会话目标控制器。"""
    global GOAL
    import os
    model = (evaluator_model
             or os.getenv("GOAL_EVALUATOR_MODEL_ID")
             or os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL")
             or core.MODEL)
    evaluator = goal_module.PromptGoalEvaluator(client=core.client, model=model)
    block_cap = int(os.getenv("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP",
                              str(goal_module.DEFAULT_STOP_HOOK_BLOCK_CAP)))
    GOAL = goal_module.GoalController(evaluator=evaluator, block_cap=block_cap)
    return GOAL


configure_goal()


def spawn_teammate(name: str, role: str, prompt: str,
                   task_id: str | None = None,
                   require_plan: bool = False) -> str:
    return teams.spawn_teammate_thread(name, role, prompt, task_id, require_plan)


# -- 上下文 --


def remember_after_turn(messages: list) -> None:
    """从已完成的对话中提取并整理长期记忆。

    Args:
        messages: 已完成本轮处理的完整对话历史。

    Returns:
        ``None``。
    """
    if memory_runtime.extract_memories(messages):
        memory_runtime.consolidate_memories()


# -- Agent 主循环 --

rounds_since_todo = 0
agent_lock = threading.Lock()


# 每次调用模型前，按从轻到重的顺序控制对话历史大小。
def prepare_context(messages: list, active_request: str) -> list:
    """压缩对话历史，避免下一次模型调用超出上下文限制。

    Args:
        messages: 当前对话历史，会被原地压缩。
        active_request: 当前用户请求，历史摘要会保留该请求。

    Returns:
        原来的 ``messages`` 列表对象。
    """
    # 先缩减最新一批过大的工具结果。
    messages[:] = compaction.tool_result_budget(messages)
    # 消息数量过多时归档中间的旧消息，保留头尾。
    messages[:] = compaction.snip_compact(messages)
    # 前两步后超过触发线，才处理更早的工具结果。
    if compaction.estimate_size(messages) > core.COMPACT_TRIGGER:
        # 将旧工具结果尽量压到目标值，给后续消息留出空间。
        target = core.COMPACT_TARGET
        # 将模型已看过的旧工具结果替换成磁盘路径。
        messages[:] = compaction.micro_compact(messages, target)
        # 旧结果缩减后仍超过触发线，就继续缩小较大的工具结果。
        if compaction.estimate_size(messages) > core.COMPACT_TRIGGER:
            # 为大结果保存完整输出，只在上下文中保留预览。
            messages[:] = compaction.fit_tool_results(messages, target)
    # 工具结果缩减后仍超过触发线，最后才调用模型生成历史摘要。
    if compaction.estimate_size(messages) > core.COMPACT_TRIGGER:
        # 用摘要替换历史，同时保留当前用户请求。
        messages[:] = compaction.compact_history(messages, active_request)
    # 返回原列表；切片赋值使调用方持有的引用保持有效。
    return messages


def build_user_content(results: list[dict]) -> list[dict]:
    """组合工具结果和后台任务通知。

    Args:
        results: 当前批次的工具结果列表。

    Returns:
        可直接放入下一条 ``user`` 消息的新内容列表。
    """
    # 工具结果和已完成的后台通知都作为 user 侧内容返回模型，沿用 tool_result 反馈流程。
    content = list(results)
    for note in scheduler.collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list):
    """把已完成的后台任务通知追加到对话历史。

    Args:
        messages: 当前对话历史，会在有通知时原地追加消息。

    Returns:
        ``None``。
    """
    notes = scheduler.collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def call_llm(messages: list, context: runtime_context.RuntimeContext, tools: list,
             state: recovery.RecoveryState, max_tokens: int):
    """调用模型并返回响应。

    Args:
        messages: 当前对话历史。
        context: 用于生成系统提示词的运行时上下文。
        tools: 本轮可用的工具描述列表。
        state: 本轮模型重试和恢复状态。
        max_tokens: 本次响应的 token 上限。

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
            max_tokens=max_tokens),
        state)


def evaluate_goal(messages: list):
    """评估当前会话目标是否满足，并转换为本轮停止决定。
    Args:
        messages: 当前完整对话历史，供目标评估器判断完成条件。
    Returns:
        ``StopDecision`` 目标决定；没有活动目标或评估失败时返回 ``None``。
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
        print(f"  \033[31m[goal] evaluator error: "
              f"{type(exc).__name__}: {exc}\033[0m")
        # 返回 None，使本轮按未设置目标的方式结束。
        return None


# Agent 主循环：准备上下文、调用模型、执行工具并回填结果，直到本轮结束。
def agent_loop(messages: list, active_request: str):
    """执行 Agent 主循环，直到模型完成回答或本轮无法继续。
    Args:
        messages: 当前对话历史，会被原地追加助手响应和工具结果。
        active_request: 当前用户请求，供历史压缩和任务续做使用。
    Returns:
        ``None``。执行结果通过更新 ``messages`` 和相关运行时状态交付。
    """
    # TODO 提醒计数在多轮调用之间共享。
    global rounds_since_todo
    # 取得当前工具描述和工具名对应的处理函数。
    tools, handlers = assemble_tool_pool()
    # 保存本轮 API 重试、模型降级和上下文恢复状态。
    state = recovery.RecoveryState()
    # 首次调用使用默认输出 token 上限。
    max_tokens = core.DEFAULT_MAX_TOKENS
    # 管理本轮手动压缩的工具响应和执行时机。
    manual_compaction = compaction.ManualCompaction()
    # 暂存已取出、尚未确认交付的定时任务。
    unacknowledged_cron_jobs: list[scheduler.CronJob] = []

    # 每次迭代完成一次模型调用，并处理它返回的文本或工具请求。
    while True:
        # 从队列中取出本次到期的定时任务。
        fired = scheduler.consume_cron_queue()
        # 调用模型成功前保留这些任务，以便失败时重新入队。
        unacknowledged_cron_jobs.extend(fired)
        # 将每个定时任务作为一条用户消息送入对话历史。
        for job in fired:
            # 模型通过用户消息读取定时任务内容。
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            # 在终端显示截短的注入提示。
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
        # 有定时任务时也把它们纳入压缩摘要使用的当前请求。
        if fired:
            # 将本批定时任务合并成多行请求。
            scheduled_requests = "\n".join(
                f"Run scheduled task: {job.prompt}" for job in fired)
            # 保留原请求，并追加定时任务内容。
            active_request = f"{active_request}\n{scheduled_requests}".strip()
        # 将已完成的后台任务通知加入对话历史。
        inject_background_notifications(messages)

        # 连续三次工具调用未更新 TODO 时，提醒模型更新。
        if rounds_since_todo >= 3:
            # 把提醒作为用户消息加入历史。
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            # 本次提醒后重新累计调用次数。
            rounds_since_todo = 0

        # 按上下文预算裁剪或压缩历史消息。
        prepare_context(messages, active_request)
        # 根据最新消息刷新记忆、MCP 和队友上下文。
        context = runtime_context.build_context(messages)
        # 每轮重新读取工具池，包含运行中新增的 MCP 工具。
        tools, handlers = assemble_tool_pool()
        # 手动压缩完成后不再向模型提供 compact 工具。
        tools = manual_compaction.available_tools(tools)

        # 调用模型，并在失败时区分上下文超限与其他错误。
        try:
            # 重试策略和模型降级由 call_llm 内部处理。
            response = call_llm(messages, context, tools, state, max_tokens)
        # 仅在重试策略仍无法完成调用时进入此分支。
        except Exception as e:
            # 首次遇到提示过长错误时压缩历史，再重新调用模型。
            if recovery.is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                # 原地替换历史，保留调用方持有的列表引用。
                messages[:] = compaction.reactive_compact(messages, active_request)
                # 标记已尝试恢复，避免同一错误反复压缩。
                state.has_attempted_reactive_compact = True
                # 返回循环顶部重新准备上下文。
                continue
            # 调用最终失败时，将未确认的定时任务重新放回队列。
            scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
            # 将错误写入历史，供外层向用户展示。
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            # 释放已完成任务的 agent 分配记录。
            tasks.release_completed_assignment("agent")
            # 结束本轮交互，不继续执行工具。
            return

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

        # 输出达到 token 上限时，先提高上限，再尝试续写。
        if response.stop_reason == "max_tokens":
            # 尚未提高过上限时，使用更大的上限重试当前调用。
            if not state.has_escalated:
                # 设置升级后的输出 token 上限。
                max_tokens = core.ESCALATED_MAX_TOKENS
                # 防止下一次超限时重复升级。
                state.has_escalated = True
                # 在终端显示本次重试的上限。
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                # 重新调用模型，暂不把截断响应写入历史。
                continue
            # 升级后仍截断时，先保存模型已生成的内容。
            messages.append({"role": "assistant", "content": response.content})
            # 在允许的恢复次数内，请求模型从截断处继续。
            if state.recovery_count < core.MAX_RECOVERY_RETRIES:
                # 添加续写提示，作为下一次模型调用的用户消息。
                messages.append({"role": "user", "content": core.CONTINUATION_PROMPT})
                # 记录已使用的一次续写机会。
                state.recovery_count += 1
                # 返回循环顶部继续调用模型。
                continue
            # 续写机会耗尽时释放已完成的分配记录。
            tasks.release_completed_assignment("agent")
            # 停止本轮交互。
            return

        # 本次响应未截断，下一次调用恢复默认 token 上限。
        max_tokens = core.DEFAULT_MAX_TOKENS
        # 清除本轮上限升级标记。
        state.has_escalated = False
        # 保存模型响应，供工具结果和后续调用引用。
        messages.append({"role": "assistant", "content": response.content})
        # 没有工具请求时，模型已完成当前一轮回答。
        if not subagent.has_tool_use(response.content):
            # 通知 Stop 钩子本轮模型输出已完成。
            hooks.trigger_hooks("Stop", messages)
            # 检查当前目标是否允许结束。
            decision = evaluate_goal(messages)
            # 目标未达成时，反馈缺失条件并继续调用模型。
            if decision is not None and decision.action == "block":
                # 读取仍在生效的目标条件。
                condition = GOAL.active.condition if GOAL.active else ""
                # 在终端显示要求继续的原因。
                print(f"  \033[35m[goal] continue: "
                      f"{decision.reason[:80]}\033[0m")
                # 将目标条件和评估结果写回历史。
                messages.append({"role": "user", "content": (
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
                print(f"  \033[35m[goal] {decision.action}: "
                      f"{decision.reason}\033[0m")
            # 从已完成的对话中提取并整理长期记忆。
            remember_after_turn(messages)
            # 释放已完成任务的 agent 分配记录。
            tasks.release_completed_assignment("agent")
            # 本轮回答结束。
            return

        # 本次响应包含工具调用，准备收集工具执行结果。
        results = []
        # 按模型返回顺序处理每个内容块。
        for block in response.content:
            # 非工具块保留在助手消息中，无需执行。
            if block.type != "tool_use":
                # 跳过文本等非工具内容。
                continue
            # 在终端显示当前工具名称。
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
            # 钩子拒绝调用时，将原因作为工具结果返回。
            if blocked:
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
                    # 告知模型结果稍后通过任务通知送达。
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                # 将启动异常转换成模型可读的结果。
                except Exception as exc:
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
            output = file_tools.call_tool_handler(handler, block.input, block.name)
            # 执行完成后通知 PostToolUse 钩子。
            hooks.trigger_hooks("PostToolUse", block, output)
            # 在终端显示截短的执行结果。
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
        messages.append({"role": "user", "content": build_user_content(results)})
        # 如本轮请求了手动压缩，在工具结果回填后生成摘要。
        manual_compaction.apply_after_results(messages, active_request)


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if compaction.block_type(block) == "text":
                core.terminal_print(block["text"] if isinstance(block, dict) else block.text)


def async_event_loop(history: list, session_state: dict):
    while True:
        time.sleep(1)
        with agent_lock:
            with scheduler.cron_lock:
                fired = list(scheduler.cron_queue)
            inbox = teams.consume_lead_inbox(route_protocol=True)
            if not fired and not inbox and not scheduler.has_pending_background():
                continue
            turn_start = len(history)
            scheduled_requests = []
            for job in fired:
                scheduled_requests.append(f"Run scheduled task: {job.prompt}")
                core.terminal_print(
                    f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            if inbox:
                history.append({"role": "user",
                                "content": teams.format_team_events(inbox)})
                core.terminal_print(
                    f"  \033[33m[team auto] {len(inbox)} events\033[0m")
            active_request = (
                "\n".join(scheduled_requests)
                if scheduled_requests
                else session_state["active_user_request"]
            )
            agent_loop(history, active_request)
            print_turn_assistants(history, turn_start)
