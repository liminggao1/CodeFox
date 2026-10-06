"""The integrated harness: agent loop, workflows, and the goal Stop boundary.

This module only wires the feature modules together. Everything the model can
do lives in its own module: core (config), tasks, worktrees, task_worktrees,
skills, prompts, file_tools, teams, hooks, subagent, compaction, recovery,
scheduler, mcp_tools, and registrations. Launch through cli.py.
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
from . import memory_runtime
from . import goal as goal_module
from . import workflow

# Re-export the names cli.py and the flat build rely on.
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
    current_work_identity, _run_teammate_tool, apply_plan_response,
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
    assemble_tool_pool, connect_mcp, mcp_clients, MCPClient, MOCK_SERVERS,
    normalize_mcp_name, MCP_HOST_POLICY, mcp_tool_policies,
)
from .registrations import (  # noqa: F401
    BUILTIN_TOOLS, BUILTIN_HANDLERS, run_create_task, run_update_task,
    run_list_tasks, run_get_task, run_claim_task, run_complete_task,
    run_spawn_teammate, run_list_teammates, run_send_message,
    run_connect_mcp, run_create_worktree,
)
from .recovery import RecoveryState  # noqa: F401
from . import workflow as workflow_runtime  # noqa: F401

scheduler.restore_cron_jobs([])
try:
    from .scheduler import load_durable_jobs
    load_durable_jobs()
except Exception:
    pass

workflow.RUNNER_FACTORY = (
    lambda: workflow.AnthropicAgentRunner(core.client, core.MODEL)
)

GOAL = None


def configure_goal(evaluator_model: str | None = None):
    """Build the session goal controller the Stop boundary consults."""
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


# -- Context --


def update_context(context: dict, messages: list) -> dict:
    return {
        "memory_catalog": memory_runtime.read_memory_index(),
        "memories": memory_runtime.load_memories(messages),
        "connected_mcp": list(mcp_tools.mcp_clients.keys()),
        "active_teammates": list(teams.active_teammates.keys()),
    }


def remember_after_turn(messages: list) -> None:
    if memory_runtime.extract_memories(messages):
        memory_runtime.consolidate_memories()


# -- Agent Loop --

rounds_since_todo = 0
agent_lock = threading.Lock()


def prepare_context(messages: list, active_request: str) -> list:
    # Every LLM turn enters through the same context budget pipeline.
    messages[:] = compaction.tool_result_budget(messages)
    messages[:] = compaction.snip_compact(messages)
    if compaction.estimate_size(messages) > core.CONTEXT_LIMIT:
        target = int(core.CONTEXT_LIMIT * 0.8)
        messages[:] = compaction.micro_compact(messages, target)
        if compaction.estimate_size(messages) > core.CONTEXT_LIMIT:
            messages[:] = compaction.fit_tool_results(messages, target)
    if compaction.estimate_size(messages) > core.CONTEXT_LIMIT:
        messages[:] = compaction.compact_history(messages, active_request)
    return messages


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool results and completed background notifications are both returned to
    # the model as user-side content, matching the tool_result feedback loop.
    content = list(results)
    for note in scheduler.collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list):
    notes = scheduler.collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def call_llm(messages: list, context: dict, tools: list,
             state: recovery.RecoveryState, max_tokens: int):
    system = prompts.assemble_system_prompt(context)
    return recovery.with_retry(
        lambda: core.client.messages.create(
            model=state.current_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens),
        state)


def evaluate_goal(messages: list):
    """Ask the goal evaluator whether this stop should be allowed."""
    if GOAL is None or GOAL.active is None:
        return None
    try:
        return asyncio.run(GOAL.evaluate_after_turn(
            messages, background_running=scheduler.has_pending_background()))
    except Exception as exc:
        print(f"  \033[31m[goal] evaluator error: "
              f"{type(exc).__name__}: {exc}\033[0m")
        return None


def agent_loop(messages: list, context: dict, active_request: str):
    global rounds_since_todo
    tools, handlers = mcp_tools.assemble_tool_pool()
    state = recovery.RecoveryState()
    max_tokens = core.DEFAULT_MAX_TOKENS

    unacknowledged_cron_jobs: list[scheduler.CronJob] = []
    while True:
        # One cycle: inject scheduled/background work, prepare context, call
        # the model, execute tool_use blocks, append tool_results, repeat.
        fired = scheduler.consume_cron_queue()
        unacknowledged_cron_jobs.extend(fired)
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
        if fired:
            scheduled_requests = "\n".join(
                f"Run scheduled task: {job.prompt}" for job in fired)
            active_request = f"{active_request}\n{scheduled_requests}".strip()

        inject_background_notifications(messages)

        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        prepare_context(messages, active_request)
        context = update_context(context, messages)
        tools, handlers = mcp_tools.assemble_tool_pool()

        try:
            response = call_llm(messages, context, tools, state, max_tokens)
        except Exception as e:
            if recovery.is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = compaction.reactive_compact(messages, active_request)
                state.has_attempted_reactive_compact = True
                continue
            scheduler.restore_cron_jobs(unacknowledged_cron_jobs)
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            tasks.release_completed_assignment("agent")
            return

        try:
            scheduler.acknowledge_cron_jobs(unacknowledged_cron_jobs)
        except OSError as exc:
            core.terminal_print(f"[cron error] Delivery confirmation failed: {exc}")
        unacknowledged_cron_jobs.clear()

        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = core.ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < core.MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": core.CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            tasks.release_completed_assignment("agent")
            return

        max_tokens = core.DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})
        if not subagent.has_tool_use(response.content):
            hooks.trigger_hooks("Stop", messages)
            decision = evaluate_goal(messages)
            if decision is not None and decision.action == "block":
                condition = GOAL.active.condition if GOAL.active else ""
                print(f"  \033[35m[goal] continue: "
                      f"{decision.reason[:80]}\033[0m")
                messages.append({"role": "user", "content": (
                    "[Goal still active]\n"
                    f"Condition: {condition}\n"
                    f"Evaluator: {decision.reason}\n"
                    "Continue working and surface the missing evidence."
                )})
                continue
            if decision is not None and decision.action != "allow":
                print(f"  \033[35m[goal] {decision.action}: "
                      f"{decision.reason}\033[0m")
            remember_after_turn(messages)
            tasks.release_completed_assignment("agent")
            return

        results = []
        compact_requested = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if block.name == "compact":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "[Compaction requested. This completed turn will be summarized.]",
                })
                compact_requested = True
                continue

            blocked = hooks.trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            if scheduler.should_run_background(block.name, block.input):
                try:
                    bg_id = scheduler.start_background_task(block, handlers)
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                except Exception as exc:
                    output = (f"Error: Failed to start background task: "
                              f"{type(exc).__name__}: {exc}")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                continue

            handler = handlers.get(block.name)
            output = file_tools.call_tool_handler(handler, block.input, block.name)
            hooks.trigger_hooks("PostToolUse", block, output)
            print(str(output)[:300])

            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": build_user_content(results)})
        if compact_requested:
            messages[:] = compaction.compact_history(messages, active_request)


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if compaction.block_type(block) == "text":
                core.terminal_print(block["text"] if isinstance(block, dict) else block.text)


def async_event_loop(history: list, context: dict, session_state: dict):
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
            agent_loop(history, context, active_request)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)
