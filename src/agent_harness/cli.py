#!/usr/bin/env python3
"""agent-harness CLI.

Interactive session: prompts go to the integrated host loop, with /goal handled
locally by the goal controller.

  agent-harness                    interactive session
  python -m agent_harness          same, without installing the console script
  agent-harness workflow demo      run the sample workflow with the mock runner
  agent-harness workflow resume    resume the last workflow run from its journal
"""

from __future__ import annotations

import asyncio
import sys
import threading

from . import goal as goal_module
from . import host, workflow


def handle_goal_command(query: str) -> tuple[bool, str]:
    """Handle /goal locally. Return (handled, replacement_query)."""
    stripped = query.strip()
    if stripped == "/goal":
        print(host.GOAL.status(0))
        return True, ""
    if not stripped.startswith("/goal "):
        return False, query

    argument = stripped[6:].strip()
    if not argument:
        print(host.GOAL.status(0))
        return True, ""
    if argument.lower() in goal_module.CLEAR_ALIASES:
        print(host.GOAL.clear())
        return True, ""
    host.GOAL.set_goal(argument)
    print(f"  \033[35m[goal] set: {argument}\033[0m")
    return False, argument


def run_cli() -> None:
    host.CLI_ACTIVE = True
    host.start_runtime_services()
    print("agent-harness: agent loop + tools + workflow + goal")
    print("Enter a question, press Enter to send. Type q to quit.")
    print("Set a completion condition with /goal <condition>.\n")

    history: list = []
    context = host.update_context({}, history)
    session_state = {"active_user_request": "(no active user request)"}
    threading.Thread(
        target=host.async_event_loop,
        args=(history, context, session_state),
        daemon=True,
    ).start()

    while True:
        try:
            query = host.CONSOLE.ask()
        except (EOFError, KeyboardInterrupt):
            break
        stripped = query.strip()
        if stripped.lower() in ("q", "exit", ""):
            break

        handled, query = handle_goal_command(stripped)
        if handled:
            print()
            continue

        with host.agent_lock:
            host.trigger_hooks("UserPromptSubmit", query)
            turn_start = len(history)
            session_state["active_user_request"] = query
            history.append({"role": "user", "content": query})
            host.agent_loop(history, context, query)
            context = host.update_context(context, history)
            host.print_turn_assistants(history, turn_start)
        print()


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "workflow":
        # The demo path stays offline; only the interactive host calls the API.
        workflow.RUNNER_FACTORY = workflow.MockAgentRunner
        asyncio.run(workflow.run_demo(argv[1:]))
        return
    run_cli()


if __name__ == "__main__":
    main(sys.argv[1:])
