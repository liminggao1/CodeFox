"""Exercise the optional UI against the real offline runtime."""

import agent_harness.core as core
import agent_harness.runtime.scheduler as scheduler
import agent_harness.agents.subagent as subagent_runtime


import asyncio
import threading
import time
from xml.etree import ElementTree

import pytest

pytest.importorskip("textual")

from textual.widgets import Markdown, Static, TextArea
from textual.widgets.text_area import Selection
from textual.containers import VerticalScroll
from agent_harness.frontends.tui.app import HarnessApp
from agent_harness.frontends.tui.widgets import ApprovalCard, CommandCandidates, SessionScreen
from agent_harness.runtime.interaction import ExecutionContext
from support.fakes import blocking_client, response, tool


async def wait_for(pilot, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            await pilot.pause()
            return
        await pilot.pause(0.05)
    raise AssertionError("UI did not reach the expected state")


def approval_button(app, decision):
    return next(card for card in app.query(ApprovalCard)
                if card.request_id == app.approval.request_id).query_one("." + decision)


def test_tui_dialogue_approval_sessions_and_shutdown(runtime_factory):
    runtime, host = runtime_factory([response("准备", tools=[tool("bash", command="printf hello")], stop="tool_use"), response("**你好**")])
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(110, 35)) as pilot:
            editor = app.query_one("#input", TextArea)
            editor.load_text("你好\n请执行")
            await pilot.click("#send")
            await wait_for(pilot, lambda: app.approval is not None)
            assert app.busy and app.turn_id
            await pilot.click(approval_button(app, "approve"))
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
            await wait_for(pilot, lambda: app.approval is not None)
            editor.load_text("next draft")
            await pilot.press("ctrl+c")
            await wait_for(pilot, lambda: not app.busy)
            assert app.approval is None and len(app.screen_stack) == 1
            assert editor.text == "next draft"
            assert not (core.WORKDIR / "unwanted").exists()
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_ignores_old_generation_after_resume(runtime_factory):
    runtime, host = runtime_factory()
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    target["messages"] = [{"role": "assistant", "content": [{"type": "text", "text": "restored"}]}]
    target["events"] = [{"id": "restored-record", "at": target["created_at"], "kind": "display",
                         "type": "assistant_message", "source": "lead", "turn_id": None,
                         "payload": {"text": "restored"}}]
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
    monkeypatch.setattr(core, "CRON_POLL_SECONDS", 0.01)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/resume " + target["id"])
            await wait_for(pilot, lambda: app.generation == 2 and not app.busy)
            with scheduler.cron_lock:
                scheduler.schedule_job("* * * * *", "cannot persist", recurring=False)
                def fail():
                    raise OSError("cron disk unavailable")
                monkeypatch.setattr(scheduler, "save_durable_jobs", fail)
            await wait_for(pilot, lambda: any("cron disk unavailable" in log for log in logs))
            assert app.generation == 2 and app.snapshot["session_id"] == target["id"]
            assert not scheduler.cron_queue
            old = ExecutionContext(runtime.channel, "old", 1, "old-turn")
            old.publish("assistant_message", text="stale")
            await pilot.pause(0.15)
            assert not app.query(Markdown)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_subagent_conversation_updates_while_background_lead_is_paused(runtime_factory):
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
            await wait_for(pilot, lambda: any(child["status"] == "working" for child in app.snapshot.get("subagents", [])))
            await pilot.press("ctrl+c")
            lead_release.set()
            await wait_for(pilot, lambda: not app.busy)
            child_release.set()
            await wait_for(pilot, lambda: any(child["status"] == "idle" for child in app.snapshot["subagents"]))
            assert any("helper · idle" in str(widget.content) for widget in app.query(Static))
            assert runtime.turns.background_paused and not app.busy
            assert lead_calls == [True, True]
            subagent_runtime.run_request_shutdown("helper").content
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
            assert any("失败" in str(widget.content) for widget in app.query(Static))
            assert not app.query("#status")
            app._submit("next")
            await wait_for(pilot, lambda: len(app.query(Markdown)) == 1 and not app.busy)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


async def command_draft(app, pilot, text):
    editor = app.query_one("#input", TextArea)
    editor.focus()
    editor.load_text(text)
    editor.move_cursor((0, len(text)))
    await pilot.pause()
    return editor


def test_tui_approval_after_large_batch_ignores_later_logs(runtime_factory, monkeypatch):
    from agent_harness.runtime.interaction import current_context
    calls = []
    def create(**kwargs):
        if not calls:
            calls.append(True)
            for number in range(205):
                current_context().publish("log", text=f"Before {number}")
            return response("Before tool", tools=[tool("bash", command="printf hello")], stop="tool_use")
        return response("After")
    runtime, host = runtime_factory(client=create)
    app = HarnessApp(runtime)
    shown = []
    event_handler = app._event
    cron = ExecutionContext(runtime.channel, None, 0, source="cron", scope="runtime")
    async def display(event):
        await event_handler(event)
        shown.append(event)
        if app.busy and app.approval is None:
            # A stream of later logs must not require an empty queue to show approval.
            cron.publish("log", text="Later log A")
            cron.publish("log", text="Later log B")
    monkeypatch.setattr(app, "_event", display)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("run")
            await wait_for(pilot, lambda: app.approval is not None)
            assert len([event for event in shown if event.payload.get("text", "").startswith("Before ")]) == 206
            assert any(event.type == "tool_call" for event in shown)
            await pilot.click(approval_button(app, "deny"))
            await wait_for(pilot, lambda: not app.busy)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_display_error_closes_runtime(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            async def unavailable(event):
                raise RuntimeError("display failed")
            with monkeypatch.context() as patch:
                patch.setattr(app, "_event", unavailable)
                ExecutionContext(runtime.channel, None, 1).publish("log", text="Cannot display")
                with pytest.raises(RuntimeError, match="display failed"):
                    await app.consume()
            await wait_for(pilot, lambda: app.runtime_closed)
            assert runtime.submit("next").status == "closed"
    asyncio.run(exercise())


def record_submissions(runtime, monkeypatch):
    submissions = []
    submit = runtime.submit
    def record(text):
        result = submit(text)
        submissions.append((text, result))
        return result
    monkeypatch.setattr(runtime, "submit", record)
    return submissions


def test_command_menu_navigation_fill_escape_and_editor_boundaries(runtime_factory):
    runtime, host = runtime_factory()
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(80, 25)) as pilot:
            editor = await command_draft(app, pilot, "/")
            panel = app.query_one(CommandCandidates)
            assert panel.display and panel.names == ("/goal", "/new", "/resume", "/delete", "/quit")
            assert panel.highlighted == 0 and app.focused is editor
            await pilot.press("up")
            assert panel.highlighted == 4 and app.focused is editor
            await pilot.press("down", "down", "down", "tab")
            assert editor.text == "/resume" and not app.busy
            assert panel.names == ("/resume",) and panel.highlighted == 0
            await pilot.press("escape")
            assert not panel.display and editor.text == "/resume"
            editor.move_cursor((0, 0))
            editor.move_cursor((0, len(editor.text)))
            await pilot.pause(0.1)
            assert not panel.display
            await pilot.press("backspace")
            assert panel.display and panel.names == ("/resume",)
            editor.move_cursor((0, 1))
            await pilot.pause()
            assert not panel.display
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert editor.text == ""
            await command_draft(app, pilot, "/goal")
            editor.selection = Selection((0, 0), (0, 5))
            await pilot.pause()
            assert not panel.display
            for text in ("中文 /goal", "/tmp/file", "C:\\tmp", "/goal\n多行"):
                editor.load_text(text)
                editor.move_cursor(editor.document.end)
                await pilot.pause()
                assert not panel.display
            editor.load_text("你好")
            editor.move_cursor((0, 2))
            await pilot.press("shift+enter")
            assert editor.text == "你好\n"
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert editor.text == ""
            await pilot.press("ctrl+l")
            assert editor.text == "" and not panel.display
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_command_enter_parameters_and_raw_submission(runtime_factory, monkeypatch):
    calls = []
    def create(**kwargs):
        calls.append(True)
        return response("done")
    runtime, host = runtime_factory(client=create)
    monkeypatch.setattr(host, "evaluate_goal", lambda messages: None)
    submissions = record_submissions(runtime, monkeypatch)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            panel = app.query_one(CommandCandidates)
            editor = await command_draft(app, pilot, "  /go")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert [text for text, _ in submissions] == ["  /goal"]
            assert editor.text == "" and not panel.display and not calls
            await command_draft(app, pilot, "/goal")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert len(submissions) == 2 and not calls
            await command_draft(app, pilot, "/goal 写中文报告")
            assert not panel.display
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert submissions[-1][0] == "/goal 写中文报告" and len(calls) == 1
            await command_draft(app, pilot, "/goal clear")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert not host.GOAL.snapshot()["active"] and len(calls) == 1
            count = len(submissions)
            await command_draft(app, pilot, "/del")
            await pilot.press("enter")
            assert editor.text == "/delete " and len(submissions) == count
            assert "请输入会话 ID" in str(app.query_one("#command-hint", Static).content)
            await pilot.press("ctrl+enter")
            await pilot.click("#send")
            assert editor.text == "/delete " and len(submissions) == count
            target = runtime.session.store.create()
            runtime.session.store.save(target)
            await command_draft(app, pilot, "/delete " + target["id"])
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert submissions[-1][0] == "/delete " + target["id"]
            assert target["id"] not in [entry["id"] for entry in runtime.session.store.list_sessions()[0]]
            count = len(submissions)
            await command_draft(app, pilot, "/resumx")
            assert not panel.display and panel.highlighted is None
            await pilot.press("enter")
            assert len(submissions) == count and editor.text == "/resumx"
            await pilot.press("escape", "up", "down", "enter")
            assert len(submissions) == count and editor.text == "/resumx"
            await pilot.press("ctrl+enter")
            await wait_for(pilot, lambda: not app.busy)
            assert submissions[-1][0] == "/resumx" and submissions[-1][1].kind == "turn"
            await command_draft(app, pilot, "/tmp/file")
            await pilot.click("#send")
            await wait_for(pilot, lambda: not app.busy)
            assert submissions[-1][0] == "/tmp/file" and len(calls) == 3
            await command_draft(app, pilot, "/re")
            await pilot.press("ctrl+enter")
            await wait_for(pilot, lambda: not app.busy)
            assert submissions[-1][0] == "/re" and len(calls) == 4
            await command_draft(app, pilot, "/quit")
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.runtime_closed)
            assert submissions[-1][0] == "/quit"
    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(55, 18), (35, 8)])
def test_command_menu_mouse_small_layout_and_modal_focus(runtime_factory, monkeypatch, size):
    runtime, host = runtime_factory()
    submissions = record_submissions(runtime, monkeypatch)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=size) as pilot:
            editor = await command_draft(app, pilot, "/")
            panel = app.query_one(CommandCandidates)
            assert not app.query("#side")
            assert panel.region.height >= 1
            assert panel.region.bottom <= editor.region.y
            assert editor.region.bottom <= size[1] and app.focused is editor
            await pilot.press("up")
            assert panel.highlighted == 4
            await pilot.press("down")
            assert panel.highlighted == 0
            assert await pilot.click(panel, offset=(1, 0))
            await wait_for(pilot, lambda: not app.busy)
            assert [text for text, _ in submissions] == ["/goal"] and editor.text == ""
            await command_draft(app, pilot, "/re")
            await pilot.press("enter")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionScreen))
            assert not panel.display and editor.text == ""
            editor.load_text("/goal")
            editor.move_cursor((0, 5))
            count = len(submissions)
            await pilot.press("up", "down")
            assert len(submissions) == count
            app.screen.query_one("#cancel").focus()
            await pilot.press("enter")
            await wait_for(pilot, lambda: len(app.screen_stack) == 1)
            assert len(submissions) == count
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionScreen))
            await pilot.press("escape")
            await wait_for(pilot, lambda: len(app.screen_stack) == 1)
            await pilot.pause(0.1)
            assert app.focused is editor and panel.display
            assert editor.text == "/goal"
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
            assert not panel.display
    asyncio.run(exercise())


def test_command_runtime_rejection_busy_approval_and_resume_reset(runtime_factory, monkeypatch):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="printf ok")], stop="tool_use"), response()])
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(target)
    other = runtime.session.store.create()
    other["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(other)
    submissions = record_submissions(runtime, monkeypatch)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            editor = await command_draft(app, pilot, "/go")
            panel = app.query_one(CommandCandidates)
            # A background wakeup may set runtime busy before the UI sees a snapshot.
            with runtime._lifecycle:
                runtime.turns._busy = True
            await pilot.press("enter")
            assert editor.text == "/goal" and submissions[-1][1].status == "busy"
            with runtime._lifecycle:
                runtime.turns._busy = False
            await command_draft(app, pilot, "/resume " + target["id"])
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.generation == 2 and not app.busy)
            await command_draft(app, pilot, "/")
            await pilot.press("down", "escape")
            assert not panel.display
            app._submit("/resume " + other["id"])
            await wait_for(pilot, lambda: app.generation == 3 and not app.busy)
            assert panel.display and panel.highlighted == 0 and editor.text == "/"
            app._submit("run")
            await wait_for(pilot, lambda: app.approval is not None)
            editor.load_text("/goal")
            editor.move_cursor((0, 5))
            assert not panel.display
            count = len(submissions)
            await pilot.click(approval_button(app, "deny"))
            await wait_for(pilot, lambda: not app.busy)
            assert len(submissions) == count and editor.text == "/goal"
            await pilot.pause(0.1)
            assert panel.display
            runtime.close()
            app.action_command_enter()
            assert editor.text == "/goal" and submissions[-1][1].status == "closed"
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_command_draft_survives_actual_background_wakeup(runtime_factory, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def create(**kwargs):
        calls.append(True)
        if len(calls) == 2:
            started.set()
            assert release.wait(10)
        return response()
    runtime, host = runtime_factory(client=create)
    submissions = record_submissions(runtime, monkeypatch)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("initial")
            await wait_for(pilot, lambda: not app.busy)
            editor = await command_draft(app, pilot, "/goal clear")
            subagent_runtime._BUS.send("child", "lead", "Background result")
            await wait_for(pilot, lambda: started.is_set() and app.busy)
            assert not app.query_one(CommandCandidates).display
            count = len(submissions)
            await pilot.press("enter")
            assert len(submissions) == count and editor.text == "/goal clear"
            await pilot.press("shift+enter", "backspace", "ctrl+enter")
            assert editor.text == "/goal clear" and submissions[-1][1].status == "busy"
            release.set()
            await wait_for(pilot, lambda: not app.busy)
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            assert editor.text == "" and submissions[-1][0] == "/goal clear"
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_tui_approval_waits_for_preceding_event_batches(runtime_factory):
    runtime, host = runtime_factory([response(tools=[tool("bash", command="printf approved")], stop="tool_use"), response()])
    context = ExecutionContext(runtime.channel, None, 1)
    for number in range(205):
        context.publish("log", text=f"Earlier event {number}")
    runtime.submit("run")
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_for(pilot, lambda: app.approval is not None)
            await pilot.click(approval_button(app, "approve"))
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
            assert not app.query("#side")
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
def test_pure_conversation_shows_accepted_input_while_model_is_blocked(runtime_factory):
    started, release = threading.Event(), threading.Event()
    runtime, _ = runtime_factory(client=blocking_client(started, release))
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("实时输入")
            await wait_for(pilot, started.is_set)
            assert app.query_one("#session-status")
            image = ElementTree.fromstring(app.export_screenshot())
            text = "".join(image.itertext())
            assert "实时输入" in text and "正在处理请求" in text
            release.set()
            await wait_for(pilot, lambda: not app.busy)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    try:
        asyncio.run(exercise())
    finally:
        release.set()
def test_lead_stream_is_visible_before_final_confirmation_and_saved_once(runtime_factory):
    from contextlib import contextmanager
    from types import SimpleNamespace
    started, release = threading.Event(), threading.Event()
    @contextmanager
    def stream(**kwargs):
        class Stream:
            def __iter__(self):
                yield SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text"))
                yield SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="你好"))
                started.set()
                assert release.wait(10)
                yield SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="，完成"))
                yield SimpleNamespace(type="content_block_stop", index=0)
                yield SimpleNamespace(type="message_stop")
            def get_final_message(self):
                return response("你好，完成")
        yield Stream()
    runtime, _ = runtime_factory(stream=stream)
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            app._submit("流式问候")
            await wait_for(pilot, lambda: started.is_set() and len(app.query(Markdown)) == 1)
            text = "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert "你好" in text and "，完成" not in text
            snapshot = runtime.session.store.load(runtime.session.session_id)
            assert not any(e["type"] == "assistant_message" for e in snapshot["events"])
            release.set()
            await wait_for(pilot, lambda: not app.busy)
            assert len(app.query(Markdown)) == 1
            snapshot = runtime.session.store.load(runtime.session.session_id)
            assert len([e for e in snapshot["events"] if e["type"] == "assistant_message"]) == 1
            assert not any(e["type"] == "assistant_text_delta" for e in snapshot["events"])
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    try:
        asyncio.run(exercise())
    finally:
        release.set()
