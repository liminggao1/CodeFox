"""Exercise the optional UI against the real offline runtime."""

import codefox.core as core
from codefox.frontends.presentation import compact_count
import codefox.runtime.scheduler as scheduler
import codefox.agents.subagent as subagent_runtime


import asyncio
import json
import threading
import time
from xml.etree import ElementTree

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Label, ListView, Markdown, Static, TextArea
from textual.widgets.text_area import Selection
from textual.containers import VerticalScroll
from codefox.frontends.tui.app import HarnessApp
from codefox.frontends.tui.widgets import ApprovalCard, CommandCandidates, SessionScreen
from codefox.runtime.interaction import ExecutionContext
from support.fakes import response, tool


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
            requested = [record for record in runtime.session.snapshot["events"]
                         if record["type"] == "approval_requested"]
            assert len(requested) == 1
            assert requested[0]["payload"]["request_id"] == app.approval.request_id
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


def test_tui_permission_switch_persists_and_stays_visible_during_approval(runtime_factory):
    from codefox.runtime import permissions
    from codefox.frontends.tui.widgets import PermissionModeScreen
    runtime, _ = runtime_factory([response(tools=[tool("bash", command="printf done")], stop="tool_use"), response()])
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.click("#permission")
            await wait_for(pilot, lambda: isinstance(app.screen, PermissionModeScreen))
            await pilot.click("#permission-no-approval")
            await wait_for(pilot, lambda: len(app.screen_stack) == 1)
            assert str(app.query_one("#permission", Button).label) == "权限：免审批 ▾"
            loaded = permissions.PermissionSettings()
            loaded.load()
            assert loaded.mode == "no_approval"
            await pilot.click("#permission")
            await pilot.click("#permission-default")
            app.query_one("#input", TextArea).load_text("write")
            await pilot.click("#send")
            await wait_for(pilot, lambda: app.approval is not None)
            button = app.query_one("#permission", Button)
            assert str(button.label) == "权限：默认 ▾" and button.disabled
            await pilot.click(approval_button(app, "approve"))
            await wait_for(pilot, lambda: not app.busy)
            assert not button.disabled and str(button.label) == "权限：默认 ▾"
            app._submit("/new")
            await wait_for(pilot, lambda: not app.busy)
            assert runtime.permission_mode == "default"
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_resume_preserves_context_footer_and_new_session_clears_it(runtime_factory):
    from support.helpers import complete, until
    reply = response("回答")
    reply.usage.input_tokens = 3200
    runtime, _ = runtime_factory([reply])
    complete(runtime, "你好")
    session_id = runtime.session.session_id
    runtime.close()
    until(runtime, "runtime_closed")
    runtime._thread.join(3)
    restored, _ = runtime_factory()
    app = HarnessApp(restored)

    async def exercise():
        async with app.run_test(size=(110, 35)) as pilot:
            app._submit(f"/resume {session_id}")
            await wait_for(pilot, lambda: app.generation == 2 and not app.busy)
            assert app.snapshot["context_input_tokens"] == 3200
            footer = app.query_one("#bottom-status", Static)
            expected_usage = f"3.20k/{compact_count(core.CONTEXT_LIMIT)}（{3200 * 100 / core.CONTEXT_LIMIT:.0f}%）"
            assert expected_usage in str(footer.content)
            app._submit("/new")
            await wait_for(pilot, lambda: app.generation == 3 and not app.busy)
            assert app.snapshot["context_input_tokens"] == 0
            assert f"0/{compact_count(core.CONTEXT_LIMIT)}（0%）" in str(footer.content)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_tui_permission_save_failure_keeps_original_mode(runtime_factory, monkeypatch):
    from codefox.runtime import permissions
    runtime, _ = runtime_factory()
    app = HarnessApp(runtime)
    notices = []
    monkeypatch.setattr(app, "notify", lambda text, **kwargs: notices.append(text))
    def fail(mode):
        raise OSError("disk unavailable")
    monkeypatch.setattr(permissions.SETTINGS, "save", fail)
    async def exercise():
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.click("#permission")
            await pilot.click("#permission-no-approval")
            await wait_for(pilot, lambda: len(app.screen_stack) == 1)
            assert runtime.permission_mode == "default"
            assert str(app.query_one("#permission", Button).label) == "权限：默认 ▾"
            assert any("disk unavailable" in notice for notice in notices)
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


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


def test_tui_cron_error_after_resume_keeps_current_session_generation(runtime_factory, monkeypatch):
    runtime, host = runtime_factory()
    target = runtime.session.store.create()
    target["goal"] = host.GOAL.snapshot()
    runtime.session.store.save(target)
    app = HarnessApp(runtime)
    logs = []
    original_log = app.conversation.log
    async def collect_log(record):
        logs.append(record["text"])
        await original_log(record)
    monkeypatch.setattr(app.conversation, "log", collect_log)
    monkeypatch.setattr(core, "CRON_POLL_SECONDS", 0.01)
    async def exercise():
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/resume " + target["id"])
            await wait_for(pilot, lambda: app.generation == 2 and not app.busy)
            with scheduler._cron_lock:
                scheduler.schedule_job("* * * * *", "cannot persist", recurring=False)
                def fail():
                    raise OSError("cron disk unavailable")
                monkeypatch.setattr(scheduler, "save_durable_jobs", fail)
            await wait_for(pilot, lambda: any("cron disk unavailable" in log for log in logs))
            assert any("cron disk unavailable" in log["text"] for log in runtime.diagnostic_records())
            assert app.generation == 2 and app.snapshot["session_id"] == target["id"]
            assert not scheduler.has_pending_cron()
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
            return response(tools=[tool("spawn_subagent", name="helper", description="Run child work", prompt="work", mode="persistent")], stop="tool_use")
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
            # 命令失败只弹窗提示，状态行保持空白，不把错误留在状态行。
            assert any(notification.severity == "error" for notification in app._notifications)
            assert str(app.query_one("#input-status", Static).content) == ""
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


def clone_session(runtime, source_id, *, keep_goal=True):
    """复制一份可读会话文件并换新 id，作为历史列表里的目标会话。"""
    store = runtime.session.store
    payload = json.loads(store.path(source_id).read_text(encoding="utf-8"))
    target = store.create()
    payload["id"] = target["id"]
    if not keep_goal:
        payload.pop("goal")
    store.path(target["id"]).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target["id"]


def test_session_screen_requires_explicit_resume_and_guards_active(runtime_factory, monkeypatch):
    """点行只高亮，恢复要按按钮；当前会话标注占用并拒绝删除。"""
    runtime, host = runtime_factory([response("ok")])
    app = HarnessApp(runtime)
    submissions = record_submissions(runtime, monkeypatch)
    async def exercise():
        async with app.run_test(size=(110, 35)) as pilot:
            editor = app.query_one("#input", TextArea)
            editor.load_text("你好")
            await pilot.press("enter")
            await wait_for(pilot, lambda: not app.busy)
            active = runtime.session.session_id
            other = clone_session(runtime, active)
            broken = runtime.session.store.create()["id"]
            runtime.session.store.path(broken).write_text("{broken", encoding="utf-8")
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionScreen))
            screen = app.screen
            ids = [entry["id"] for entry in screen.entries]
            assert next(entry for entry in screen.entries if entry["id"] == broken)["invalid"]
            list_view = screen.query_one(ListView)
            # 标题行的返回按钮是 1 行按钮，文字不会被裁掉。
            assert screen.query_one("#cancel", Button).region.height == 1
            assert "1 个会话文件无法读取" in str(screen.query_one(".session-note", Static).content)
            # 点行只改变高亮，不触发恢复（ctrl+r 本身提交的 /resume 是列表命令）。
            listing = [text for text, _ in submissions]
            list_view.index = ids.index(other)
            await pilot.pause()
            assert isinstance(app.screen, SessionScreen)
            assert [text for text, _ in submissions] == listing
            list_view.index = ids.index(broken)
            await pilot.click("#resume")
            await pilot.pause()
            assert isinstance(app.screen, SessionScreen)
            assert [text for text, _ in submissions] == listing
            assert any("不能恢复" in notification.message for notification in app._notifications)
            await pilot.click("#delete")
            await wait_for(pilot, lambda: not runtime.session.store.path(broken).exists() and not app.busy)
            assert [text for text, _ in submissions][-1] == "/delete " + broken
            await pilot.press("ctrl+r")
            await wait_for(pilot, lambda: isinstance(app.screen, SessionScreen))
            screen = app.screen
            ids = [entry["id"] for entry in screen.entries]
            assert broken not in ids
            list_view = screen.query_one(ListView)
            # 当前会话行带占用标注，删除被拒且只弹窗。
            list_view.index = ids.index(active)
            assert "当前会话 · 正在占用" in str(list_view.highlighted_child.query_one(Label).content)
            before = len(submissions)
            await pilot.click("#delete")
            await pilot.pause()
            assert isinstance(app.screen, SessionScreen)
            assert len(submissions) == before
            assert any("不能删除" in notification.message for notification in app._notifications)
            # 显式点“恢复所选”才提交恢复命令。
            list_view.index = ids.index(other)
            await pilot.click("#resume")
            await wait_for(pilot, lambda: len(app.screen_stack) == 1)
            await pilot.pause()
            assert [text for text, _ in submissions][-1] == "/resume " + other
            await pilot.press("ctrl+d")
            await wait_for(pilot, lambda: app.runtime_closed)
    asyncio.run(exercise())


def test_command_menu_navigation_fill_escape_and_editor_boundaries(runtime_factory):
    runtime, host = runtime_factory()
    app = HarnessApp(runtime)
    async def exercise():
        async with app.run_test(size=(80, 25)) as pilot:
            editor = await command_draft(app, pilot, "/")
            panel = app.query_one(CommandCandidates)
            assert panel.display and panel.names == ("/goal", "/compact", "/new", "/resume", "/delete", "/quit")
            assert panel.highlighted == 0 and app.focused is editor
            await pilot.press("up")
            assert panel.highlighted == 5 and app.focused is editor
            await pilot.press("down", "down", "down", "down", "tab")
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
            assert panel.highlighted == 5
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


def test_tui_input_and_conversation_scroll(runtime_factory):
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
