#!/usr/bin/env python3
"""agent-harness 命令行入口。

交互会话将用户输入送入 host 主循环；/goal 由本地目标控制器处理。

  agent-harness                    启动交互会话
  python -m agent_harness          无需安装命令行脚本，也可启动交互会话
  agent-harness workflow demo      Run the workflow with an offline model client
  agent-harness workflow resume    从 journal 恢复上一次工作流
"""

from __future__ import annotations

import asyncio
import sys
import threading

from . import goal as goal_module
from . import core, host, workflow_runtime, workflow_demo
from .session_store import InteractiveSession, SessionError, SessionStore


def handle_goal_command(query: str, session: InteractiveSession | None = None) -> tuple[bool, str]:
    """在本地拦截目标状态查询、清除和设置命令。

    输入：query 为用户输入。
    返回：(handled, 后续输入)。查询或清除返回 (True, '')，不请求模型；
        设置目标返回 (False, 条件文本)，让条件继续进入主循环；普通输入原样返回。
    """
    stripped = query.strip()
    # 非 /goal 命令：原样返回，交给正常问答流程
    if not (stripped == "/goal" or stripped.startswith("/goal ")):
        return False, query
    # 提取参数：跳过 "/goal"（6 个字符）；"/goal" 本身 → 参数为空串
    argument = stripped[6:].strip()
    # 无参数（"/goal" 或 "/goal  "）→ 查看当前目标状态
    if not argument:
        result = host.GOAL.status(0)
        print(result)
        if session:
            session.record("goal_query", stripped, result)
        return True, ""
    # 参数是清除别名（clear/reset/off 等）→ 清除目标
    if argument.lower() in goal_module.CLEAR_ALIASES:
        result = host.GOAL.clear()
        print(result)
        if session:
            session.record("goal_clear", stripped, result)
        return True, ""
    # 其他参数 → 设置为新目标，并让参数继续作为用户输入走 agent 问答
    host.GOAL.set_goal(argument)
    if session:
        session.record("goal_set", argument)
    print(f"  \033[35m[goal] set: {argument}\033[0m")
    return False, argument


def handle_session_command(query: str, session: InteractiveSession,
                           session_state: dict) -> bool:
    """Handle storage commands under agent_lock, leaving invalid restores untouched."""
    parts = query.split()
    if not parts or parts[0] not in {"/resume", "/delete"}:
        return False
    command = parts[0]
    try:
        if command == "/resume" and len(parts) == 1:
            entries, errors = session.store.list_sessions()
            if not entries:
                print("No saved sessions")
            for entry in entries:
                current = " *" if entry["id"] == session.session_id else ""
                print(f"{entry['id']}{current}  {entry['updated_at']}  "
                      f"{entry['message_count']} messages  {entry['summary']}")
            for error in errors:
                print(f"[session error] Skipped {error}")
            return True
        if len(parts) != 2:
            raise SessionError(f"Usage: {command} <session_id>")
        session_id = parts[1]
        session.store.path(session_id)
        if command == "/delete":
            if session_id == session.session_id:
                raise SessionError("Cannot delete the active session")
            session.store.delete(session_id)
            print(f"[session] Deleted {session_id}")
            return True
        if session_id == session.session_id:
            print(f"[session] Already using {session_id}")
            return True
        snapshot = session.store.load(session_id)
        restored_goal = goal_module.GoalController.from_snapshot(
            host.GOAL.evaluator, snapshot["goal"], host.GOAL.block_cap)
        with host.subagent.subagent_lock:
            children_active = bool(host.subagent.active_subagents)
        if (host.scheduler.has_background_tasks() or children_active
                or host.subagent.BUS.peek("lead")):
            raise SessionError("Background work or results remain; finish them and retry /resume")
        if not session.save():
            raise SessionError("Current session could not be saved; context was not switched")
        # Retire instance-local services. Durable task/worktree records stay in place.
        host.mcp_clients.clear()
        host.rounds_since_todo = 0
        host.GOAL = restored_goal
        session_state["active_user_request"] = snapshot["active_user_request"]
        session.activate(snapshot)
        print(f"[session] Resumed {session_id} ({len(session.history)} messages)")
    except (SessionError, goal_module.GoalError, OSError) as exc:
        print(f"[session error] {exc}")
    return True



def run_cli() -> None:
    """启动交互会话，读取用户输入并驱动主 agent。

    返回：None。启动运行时服务和后台事件线程，共享历史与当前请求状态；
        本地目标命令直接处理，其他输入在 agent_lock 内进入主循环并显示回复。
        空输入、q、exit、EOF 或键盘中断会结束交互。
    """
    core.CLI_ACTIVE = True              # 标记当前处于 CLI 模式（供其他模块判断）
    host.start_runtime_services()       # 启动运行时服务（只做一次）

    # 打印欢迎/使用说明
    print("agent-harness: agent loop + tools + workflow + goal")
    print("Enter a question, press Enter to send. Type q to quit.")
    print("/goal <condition> sets a goal. /resume lists sessions; /resume <id> restores one.")
    print("/delete <id> deletes an inactive session.\n")

    history: list = []                  # 对话历史（所有 user/assistant 消息）
    session = InteractiveSession(SessionStore(core.WORKDIR), history,
                                 lambda: host.GOAL, core.terminal_print)
    stop_event = threading.Event()
    session_state = {"active_user_request": "(no active user request)",
                     "session": session, "stop_event": stop_event}
    # Old child threads do not survive process restart; their mailbox is not a new turn.
    host.subagent.consume_lead_inbox(route_protocol=False)

    # 后台守护线程：跑异步事件循环（处理定时任务/异步回调）
    event_thread = threading.Thread(
        target=host.async_event_loop,   # 在后台线程里执行这个函数
        args=(history, session_state),  # Pass history and the current request.
        daemon=True,                    # 守护线程：主程序退出它也自动结束
    )
    event_thread.start()

    # 主循环：一直读到用户退出
    try:
        while True:
            query = host.CONSOLE.ask()          # 阻塞等待用户输入一行
            stripped = query.strip()
            if stripped.lower() in ("q", "exit", ""):
                break
            if stripped.split()[0] in {"/resume", "/delete"}:
                if not host.agent_lock.acquire(blocking=False):
                    print("[session] A model turn is running; retry after it finishes")
                    continue
                try:
                    handle_session_command(stripped, session, session_state)
                finally:
                    host.agent_lock.release()
                print()
                continue
            with host.agent_lock:
                try:
                    handled, query = handle_goal_command(stripped, session)
                except goal_module.GoalError as exc:
                    session.record("goal_set", stripped, str(exc))
                    print(f"[goal error] {exc}")
                    continue
                if handled:
                    print()
                    continue
                host.trigger_hooks("UserPromptSubmit", query)
                session.finish_pending_tools()
                turn_start = len(history)
                session_state["active_user_request"] = query
                history.append({"role": "user", "content": query})
                session.record("user_input", query)
                try:
                    host.agent_loop(history, query, session.save)
                finally:
                    session.save()
                session.record("turn_complete", query)
                host.print_turn_assistants(history, turn_start)
            print()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        stop_event.set()
        host.file_tools._stop_all_shell_processes()
        with host.agent_lock:
            session.save()
            for child in host.subagent.list_subagents():
                host.subagent.run_request_shutdown(child["name"])
        event_thread.join(timeout=1)
        core.CLI_ACTIVE = False



def main(argv: list[str] | None = None) -> None:
    """根据命令行参数选择离线工作流示例或交互会话。

    输入：argv 为不含程序名的参数列表；None 时读取 sys.argv[1:]。
    Return: None. Workflow injects an offline model client; otherwise start the CLI.
    """
    # argv 没传（是 None）时，从系统命令行参数读取（去掉程序名）
    if argv is None:
        argv = sys.argv[1:]
    # 如果传了参数，且第一个参数是 "workflow"（短路求值：argv 非空才比较）
    if argv and argv[0] == "workflow":
        # Exercise the unified child runtime with a deterministic offline client.
        workflow_runtime.RUNNER_FACTORY = lambda: workflow_runtime.SubagentRunner(
            workflow_demo.DemoModelClient(), core.MODEL)
        # 异步运行 demo，把 "workflow" 之后的参数传进去
        asyncio.run(workflow_demo.run_demo(argv[1:]))
        return  # 跑完 demo 就结束，不再走下面的交互模式

    # 否则：走命令行交互模式（CLI）
    run_cli()

if __name__ == "__main__":
    # 直接运行本文件时才进入这里（被 import 则不执行）
    # 把系统参数（不含程序名）传给 main
    main(sys.argv[1:])
