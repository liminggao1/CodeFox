"""Exercise the optional UI against the real offline runtime."""

import asyncio
import threading
import time

import pytest

pytest.importorskip("textual")

from textual.widgets import Markdown, Static, TextArea
from textual.containers import VerticalScroll
from agent_harness.tui.app import HarnessApp
from agent_harness.tui.widgets import ApprovalScreen, SessionScreen
from agent_harness.interaction import ExecutionContext
from test_interactive_runtime import response, tool, runtime_factory


async def wait_for(pilot, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("UI did not reach the expected state")


def test_tui_dialogue_approval_sessions_and_shutdown(runtime_factory):
    runtime, host = runtime_factory([response("准备", tools=[tool("bash", command="printf hello")], stop="tool_use"), response("**你好**")])
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(110, 35)) as pilot:
            editor = app.query_one("#input", TextArea)
            editor.load_text("你好\n请执行")
            await pilot.click("#send")
            await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            assert app.busy and app.turn_id
            await pilot.click("#approve")
            await wait_for(pilot, lambda: not app.busy)
            await pilot.pause()
            assert len(app.query(Markdown)) == 2
            assert editor.text == ""
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionScreen))
            await pilot.press("escape")
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())
    saved = runtime.session.store.load(runtime.session.session_id)
    assert saved["messages"][0]["content"] == "你好\n请执行"
    assert saved["messages"][-1]["content"][0]["text"] == "**你好**"


def test_tui_stop_releases_approval_and_preserves_draft(runtime_factory):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="touch unwanted")], stop="tool_use")])
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            editor = app.query_one("#input", TextArea)
            editor.load_text("run")
            await pilot.click("#send")
            await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            editor.load_text("next draft")
            await pilot.press("ctrl+c")
            await wait_for(pilot, lambda: not app.busy)
            assert not isinstance(app.screen, ApprovalScreen)
            assert editor.text == "next draft"
            assert not (host.core.WORKDIR / "unwanted").exists()
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_ignores_old_generation_after_resume(runtime_factory):
    runtime, host = runtime_factory()
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    target["messages"] = [{"role": "assistant", "content": [{"type": "text", "text": "restored"}]}]
    runtime.session.store.save(target)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/resume " + target["id"])
            await wait_for(pilot, lambda: app.generation == 2 and not app.busy)
            old = ExecutionContext(runtime.channel, "old", 1, "old-turn")
            old.publish("assistant_message", text="stale")
            await pilot.pause(0.15)
            assert len(app.query(Markdown)) == 1
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_cron_error_after_resume_keeps_current_session_generation(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(target)
    app = HarnessApp(runtime)
    logs = []
    monkeypatch.setattr(app, "_log", logs.append)
    monkeypatch.setattr(host.core, "CRON_POLL_SECONDS", 0.01)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/resume " + target["id"])
            await wait_for(pilot, lambda: app.generation == 2 and not app.busy)
            with host.scheduler.cron_lock:
                host.schedule_job("* * * * *", "cannot persist", recurring=False)
                def fail():
                    raise OSError("cron disk unavailable")
                monkeypatch.setattr(host.scheduler, "save_durable_jobs", fail)
            await wait_for(pilot, lambda: any("cron disk unavailable" in log for log in logs))
            assert app.generation == 2 and app.snapshot["session_id"] == target["id"]
            assert not host.scheduler.cron_queue
            old = ExecutionContext(runtime.channel, "old", 1, "old-turn")
            old.publish("assistant_message", text="stale")
            await pilot.pause(0.15)
            assert not app.query(Markdown)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_subagent_panel_updates_while_background_lead_is_paused(runtime_factory):
    child_started, child_release = threading.Event(), threading.Event()
    lead_started, lead_release = threading.Event(), threading.Event()
    lead_calls = []
    def create(**kwargs):
        if "You are 'helper'" in str(kwargs.get("system")):
            child_started.set()
            assert child_release.wait(5)
            return response("child done")
        lead_calls.append(True)
        if len(lead_calls) == 1:
            return response(tools=[tool("spawn_subagent", name="helper", prompt="work", mode="persistent")], stop="tool_use")
        lead_started.set()
        assert lead_release.wait(5)
        return response("lead done")
    runtime, host = runtime_factory(client=create)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("work")
            await wait_for(pilot, lambda: child_started.is_set() and lead_started.is_set())
            await wait_for(pilot, lambda: any(child["status"] == "working" for child in app.snapshot["subagents"]))
            await pilot.press("ctrl+c")
            lead_release.set()
            await wait_for(pilot, lambda: not app.busy)
            child_release.set()
            await wait_for(pilot, lambda: any(child["status"] == "idle" for child in app.snapshot["subagents"]))
            assert "helper · idle" in str(app.query_one("#state", Static).content)
            assert runtime._background_paused and not app.busy
            assert lead_calls == [True, True]
            host.run_request_shutdown("helper")
            await wait_for(pilot, lambda: not app.snapshot["subagents"])
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    try:
        asyncio.run(exercise())
    finally:
        lead_release.set()
        child_release.set()


def test_tui_failed_command_finishes_status_and_accepts_next_input(runtime_factory):
    runtime, host = runtime_factory()
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/resume invalid")
            await wait_for(pilot, lambda: not app.busy)
            assert "失败" in str(app.query_one("#status", Static).content)
            assert "执行中" not in str(app.query_one("#status", Static).content)
            app._submit("next")
            await wait_for(pilot, lambda: len(app.query(Markdown)) == 1 and not app.busy)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_approval_waits_for_preceding_event_batches(runtime_factory):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="printf approved")], stop="tool_use"), response()])
    context = ExecutionContext(runtime.channel, None, 1)
    for number in range(205):
        context.publish("log", text=f"Earlier event {number}")
    runtime.submit("run")
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.click("#approve")
            await wait_for(pilot, lambda: not app.busy)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())
    results = [block for message in runtime.session.history if isinstance(message["content"], list)
               for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_result"]
    assert "approved" in results[0]["content"]


def test_tui_narrow_layout_input_and_conversation_scroll(runtime_factory):
    runtime, host = runtime_factory([response("\n\n".join(f"第 {number} 段" for number in range(80)))])
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(80, 25)) as pilot:
            editor = app.query_one("#input", TextArea)
            editor.load_text(" \n ")
            await pilot.press("ctrl+enter")
            assert editor.text == " \n " and not app.busy
            await pilot.press("ctrl+l")
            assert not editor.text
            side = app.query_one("#side")
            assert not side.display
            await pilot.press("f2")
            assert side.display
            await pilot.press("f2")
            editor.load_text("你好\n多行请求")
            await pilot.press("ctrl+enter")
            await wait_for(pilot, lambda: len(app.query(Markdown)) == 1 and not app.busy)
            await pilot.pause()
            chat = app.query_one("#chat", VerticalScroll)
            assert chat.max_scroll_y > 0
            chat.focus()
            await pilot.press("home")
            await pilot.pause()
            assert chat.scroll_y == 0
            await pilot.press("end")
            await pilot.pause()
            assert chat.scroll_y > 0
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())
