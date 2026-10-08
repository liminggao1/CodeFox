#!/usr/bin/env python3
"""agent-harness 命令行入口。

交互会话将用户输入送入 host 主循环；/goal 由本地目标控制器处理。

  agent-harness                    启动交互会话
  python -m agent_harness          无需安装命令行脚本，也可启动交互会话
  agent-harness workflow demo      用模拟 runner 运行示例工作流
  agent-harness workflow resume    从 journal 恢复上一次工作流
"""

from __future__ import annotations

import asyncio
import sys
import threading

from . import goal as goal_module
from . import core, host, workflow


def handle_goal_command(query: str) -> tuple[bool, str]:
    """在本地处理 /goal，返回（是否已处理，替换后的输入）。"""
    stripped = query.strip()
    # 非 /goal 命令：原样返回，交给正常问答流程
    if not (stripped == "/goal" or stripped.startswith("/goal ")):
        return False, query
    # 提取参数：跳过 "/goal"（6 个字符）；"/goal" 本身 → 参数为空串
    argument = stripped[6:].strip()
    # 无参数（"/goal" 或 "/goal  "）→ 查看当前目标状态
    if not argument:
        print(host.GOAL.status(0))
        return True, ""
    # 参数是清除别名（clear/reset/off 等）→ 清除目标
    if argument.lower() in goal_module.CLEAR_ALIASES:
        print(host.GOAL.clear())
        return True, ""
    # 其他参数 → 设置为新目标，并让参数继续作为用户输入走 agent 问答
    host.GOAL.set_goal(argument)
    print(f"  \033[35m[goal] set: {argument}\033[0m")
    return False, argument



def run_cli() -> None:
    """交互式命令行主循环：读用户输入，驱动 agent 回答，直到退出。"""
    core.CLI_ACTIVE = True              # 标记当前处于 CLI 模式（供其他模块判断）
    host.start_runtime_services()       # 启动运行时服务（只做一次）

    # 打印欢迎/使用说明
    print("agent-harness: agent loop + tools + workflow + goal")
    print("Enter a question, press Enter to send. Type q to quit.")
    print("Set a completion condition with /goal <condition>.\n")

    history: list = []                  # 对话历史（所有 user/assistant 消息）
    session_state = {"active_user_request": "(no active user request)"}  # 会话状态

    # 后台守护线程：跑异步事件循环（处理定时任务/异步回调）
    threading.Thread(
        target=host.async_event_loop,   # 在后台线程里执行这个函数
        args=(history, session_state),  # Pass history and the current request.
        daemon=True,                    # 守护线程：主程序退出它也自动结束
    ).start()

    # 主循环：一直读到用户退出
    while True:
        try:
            query = host.CONSOLE.ask()          # 阻塞等待用户输入一行
        except (EOFError, KeyboardInterrupt):   # Ctrl+D / Ctrl+C → 退出
            break
        stripped = query.strip()                # 去掉首尾空格
        if stripped.lower() in ("q", "exit", ""):  # q / exit / 空输入 → 退出
            break
        # 处理 /goal 这类斜杠命令；如果已被处理就跳过本轮问答
        handled, query = handle_goal_command(stripped)
        if handled:
            print()
            continue
        # 加锁：保证同一时刻只有本线程在操作 agent（避免与后台线程竞争）
        with host.agent_lock:
            host.trigger_hooks("UserPromptSubmit", query)   # 通知钩子：用户提交了问题
            turn_start = len(history)                       # 记住本轮开始前的历史长度
            session_state["active_user_request"] = query    # 更新当前活跃请求
            history.append({"role": "user", "content": query})  # 用户消息存入历史
            host.agent_loop(history, query)  # Append responses and tool results.
            host.print_turn_assistants(history, turn_start) # 只打印本轮新增的助手回复
        print()                                             # 空行分隔，方便阅读



def main(argv: list[str] | None = None) -> None:
    """程序总入口：根据命令行参数决定跑 demo 还是交互模式。"""
    # argv 没传（是 None）时，从系统命令行参数读取（去掉程序名）
    if argv is None:
        argv = sys.argv[1:]
    # 如果传了参数，且第一个参数是 "workflow"（短路求值：argv 非空才比较）
    if argv and argv[0] == "workflow":
        # demo 模式：保持离线，不调用真实 API。
        # 把 RUNNER_FACTORY 替换成"模拟的 Runner"，用于测试/演示
        workflow.RUNNER_FACTORY = workflow.MockAgentRunner
        # 异步运行 demo，把 "workflow" 之后的参数传进去
        asyncio.run(workflow.run_demo(argv[1:]))
        return  # 跑完 demo 就结束，不再走下面的交互模式

    # 否则：走命令行交互模式（CLI）
    run_cli()

if __name__ == "__main__":
    # 直接运行本文件时才进入这里（被 import 则不执行）
    # 把系统参数（不含程序名）传给 main
    main(sys.argv[1:])
