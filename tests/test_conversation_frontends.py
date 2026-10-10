"""Frontend replay uses backend records, never reconstructs model history."""

import asyncio
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from agent_harness.frontends.cli import CLIAdapter
from agent_harness.runtime.interaction import InteractionChannel


def record(number, kind, payload, source="lead", turn="turn"):
    return {"id": f"{number:032x}", "at": "2026-10-09T00:00:00+00:00", "kind": "display",
            "type": kind, "source": source, "turn_id": turn, "payload": payload}


def event(kind, payload):
    return SimpleNamespace(type=kind, payload=payload, scope="session", generation=1,
                           session_id="test", sequence=1, turn_id="turn", source="lead")


def test_cli_record_replay_details_status_and_dedup():
    output = []
    adapter = CLIAdapter(None, write=output.append)
    records = [record(1, "user_message", {"text": "request"}),
               record(2, "tool_call", {"id": "tool", "name": "read_file", "input": {"path": "文件.txt"}}),
               record(3, "tool_result", {"tool_use_id": "tool", "content": "Error is normal content\n" + "x" * 500}),
               record(4, "todo_updated", {"todos": [{"status": "completed", "content": "inspect"}]}),
               record(5, "approval_requested", {"request_id": "historical", "tool_name": "bash", "arguments": {},
                                                 "historical": True, "interrupted": True}),
               record(6, "turn_finished", {"outcome": {"status": "completed"}, "summary": {
                   "elapsed_seconds": 1.5, "tool_calls": 1, "approvals": 0,
                   "lead_usage": {"model_calls": 2, "input_tokens": 20, "output_tokens": 10}}})]
    adapter.render(event("session_changed", {"records": records}))
    adapter.render(event("display_record", {"record": records[2]}))
    adapter.render(event("assistant_message", {"text": "raw duplicate"}))
    text = "\n".join(output)
    assert len(output) == len(records)
    assert "read_file · 已返回结果" in text and "Error is normal content" in text and "x" * 500 not in text
    assert "文件.txt" in text and "上次中断，未继续执行" in text
    assert "raw duplicate" not in text
    assert "1.5s" in text and "请求 2" in text
    missing = record(7, "tool_call", {"id": "unfinished", "name": "write_file", "input": {},
                                       "historical": True, "result_missing": True})
    adapter.render(event("display_record", {"record": missing}))
    assert output[-1] == "结果未记录，请核对当前状态"


class DisplayRuntime:
    def __init__(self):
        self.channel = InteractionChannel()
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


def test_tui_record_window_expansion_history_and_dedup():
    pytest.importorskip("textual")
    from textual.widgets import Button, Collapsible, Static
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import ApprovalCard, ConversationRecord

    app = HarnessApp(DisplayRuntime())
    records = [record(index + 1, "user_message", {"text": f"message {index}"}) for index in range(90)]
    records += [record(91, "tool_call", {"id": "tool", "name": "read_file", "input": {"path": "文件.txt"},
                                        "result_missing": True}),
                record(92, "tool_result", {"tool_use_id": "tool", "content": "detailed result", "status": "success"}),
                record(93, "approval_requested", {"request_id": "old", "tool_name": "bash", "arguments": {},
                                                   "historical": True, "interrupted": True})]

    async def exercise():
        async with app.run_test(size=(55, 22)) as pilot:
            await app._event(event("session_changed", {"records": records}))
            await pilot.pause()
            assert len(app.query(ConversationRecord)) == 80
            assert app.query_one("#earlier", Button).display
            assert not app.query_one(ApprovalCard).query(Button)
            details = app.query(Collapsible)
            assert len(details) == 3 and all(detail.collapsed for detail in details)
            assert any("结果未记录" in str(widget.content) for widget in app.query(Static))
            details[1].collapsed = False
            await pilot.pause()
            assert any("detailed result" in str(widget.content) for widget in details[1].query(Static))
            await app._event(event("display_record", {"record": records[-1]}))
            assert len(app.records) == 93
            await pilot.click("#earlier")
            await pilot.pause()
            assert len(app.query(ConversationRecord)) == 93
            assert not app.query_one("#earlier", Button).display
            assert app.query_one("#input").region.bottom <= 22
    asyncio.run(exercise())


def test_tui_workbench_has_brand_state_panel_and_single_stateful_button():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import BrandBar, SessionStatePanel
    from textual.widgets import Button, Static

    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(110, 30)) as pilot:
            assert app.query_one(BrandBar)
            assert app.query_one(SessionStatePanel)
            assert app.query_one("#send", Button).label == "发送"
            assert not app.query("#stop")
            await app._event(event("state_snapshot", {
                "history": [{"role": "user", "content": "hello"}],
                "goal": None, "todos": [], "subagents": [], "model": "test-model",
                "busy": True, "closing": False,
                "lead_main_model_usage": {"model_calls": 1, "input_tokens": 2, "output_tokens": 3},
                "workdir": "/tmp/project", "task_id": "task_1", "worktree": "feature",
            }))
            await pilot.pause()
            assert app.query_one("#send", Button).label == "停止"
            state = str(app.query_one(SessionStatePanel).query_one(".state-body", Static).content)
            assert "test-model" in state and "/tmp/project" in state and "feature" in state
    import asyncio
    asyncio.run(exercise())


def test_tui_inline_approval_only_enables_delivered_request(monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import ApprovalCard
    from agent_harness.runtime.interaction import ApprovalRequest

    runtime = DisplayRuntime()
    app = HarnessApp(runtime)
    ready = []
    decisions = []
    monkeypatch.setattr(runtime.channel, "next_approval", lambda: ready.pop(0) if ready else None)
    monkeypatch.setattr(runtime.channel, "respond", lambda *args: decisions.append(args) or True)
    request = ApprovalRequest("current", "test", 1, "turn", "lead", "bash", "tool", {"command": "pwd"})

    async def exercise():
        async with app.run_test(size=(45, 18)) as pilot:
            await app.conversation.display_record(record(1, "approval_requested", {"request_id": "old", "tool_name": "bash",
                                      "arguments": {}, "historical": True, "interrupted": True}))
            await app.conversation.display_record(record(2, "approval_requested", {"request_id": "current", "tool_name": "bash",
                                      "arguments": {"command": "pwd"}}))
            await pilot.pause()
            card = next(card for card in app.query(ApprovalCard) if card.request_id == "current")
            assert all(button.disabled for button in card.query(Button))
            ready.append(request)
            await app.consume()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert not card.query_one(".approve", Button).disabled
            assert card.query_one(".approve", Button).region.y >= 2
            assert card.query_one(".approve", Button).region.bottom <= app.query_one("#input").region.y
            image = ElementTree.fromstring(app.export_screenshot())
            screen_text = "".join(image.itertext())
            assert "批准" in screen_text and "拒绝" in screen_text, screen_text
            await pilot.click(card.query_one(".approve", Button))
            await pilot.pause()
            assert decisions == [("current", 1, "approve")]
            assert all(button.disabled for button in card.query(Button))
            assert app.approval is None
    asyncio.run(exercise())
def test_tool_summary_is_visible_before_expanding_details():
    from agent_harness.frontends.tui.app import HarnessApp
    from textual.widgets import Collapsible
    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            await app.conversation.display_record(record(1, "tool_call", {"id": "read", "name": "read_file",
                                     "input": {"path": "说明.md"}}))
            await app.conversation.display_record(record(2, "tool_result", {"tool_use_id": "read", "status": "returned",
                                     "content": "读取成功\n" + "long output\n" * 100}))
            await pilot.pause()
            text = "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert "说明.md" in text and "读取成功" in text
            assert all(item.collapsed for item in app.query(Collapsible))
    asyncio.run(exercise())


def test_operation_summary_keeps_confirmed_elapsed_time():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import OperationEntry
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            from agent_harness.frontends.conversation import ConversationProjection
            projection = ConversationProjection()
            change = projection.apply_record(record(1, "execution_status", {
                "operation_id": "op", "phase": "tool", "name": "读取 README.md",
                "state": "completed", "elapsed_seconds": 3.0,
            }))[0]
            entry = OperationEntry(change["entry"])
            await app.query_one("#chat", VerticalScroll).mount(entry)
            entry.render_entry(change["entry"])
            await pilot.pause()
            text = str(entry.query_one(".operation-summary", Static).content)
            assert "耗时 3.0s" in text
    import asyncio
    asyncio.run(exercise())
def test_many_tools_share_scrollable_group_and_update_in_place():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import OperationEntry, ProcessGroup
    from textual.containers import VerticalScroll
    from textual.events import MouseScrollDown
    app = HarnessApp(DisplayRuntime())
    async def exercise():
        async with app.run_test(size=(85, 32)) as pilot:
            for index in range(20):
                await app.conversation.display_record(record(100 + index, "tool_call", {
                    "id": f"tool-{index}", "name": "read_file", "input": {"path": f"file-{index}.txt"},
                    "model_call_id": "model-1"}))
                await app.conversation.display_record(record(200 + index, "tool_result", {
                    "tool_use_id": f"tool-{index}", "content": f"result-{index}", "status": "returned",
                    "model_call_id": "model-1"}))
            await pilot.pause()
            assert len(app.query(ProcessGroup)) == 1
            assert len(app.query(OperationEntry)) == 20
            scroll = app.query_one(ProcessGroup).query_one(VerticalScroll)
            assert scroll.region.height <= 14 and scroll.max_scroll_y > 0
            scroll.scroll_home(animate=False)
            await pilot.pause()
            await pilot.hover(scroll, offset=(3, 2))
            outer = app.query_one("#chat", VerticalScroll)
            position = outer.scroll_y
            await pilot._post_mouse_events([MouseScrollDown], scroll, offset=(3, 2))
            await pilot.pause()
            assert scroll.scroll_y > 0 and outer.scroll_y == position
            bar = scroll.vertical_scrollbar
            await pilot.click(bar, offset=(0, bar.region.height - 2))
            await pilot.pause()
            assert scroll.scroll_y > 0
            scroll.scroll_end(animate=False)
            await pilot.pause()
            text = "".join(ElementTree.fromstring(app.export_screenshot()).itertext())
            assert "file-19.txt" in text and "result-19" in text
    asyncio.run(exercise())
def test_cli_stream_confirms_without_duplicate_body_and_marks_discarded_attempt():
    output, chunks = [], []
    adapter = CLIAdapter(None, write=output.append, write_chunk=chunks.append)
    adapter.render(event("assistant_text_delta", {"model_call_id": "one", "block_index": 0, "sequence": 1, "delta": "你好"}))
    adapter.render(event("log", {"text": "background update"}))
    adapter.render(event("assistant_text_delta", {"model_call_id": "one", "block_index": 0, "sequence": 2, "delta": "完成"}))
    adapter.render(event("display_record", {"record": record(1, "assistant_message", {
        "model_call_id": "one", "block_index": 0, "text": "你好完成"})}))
    adapter.render(event("display_record", {"record": record(2, "execution_status", {
        "phase": "model", "name": "请求模型", "state": "discarded", "operation_id": "old", "model_call_id": "one"})}))
    assert "你好" in "".join(chunks) and "完成" in "".join(chunks)
    assert not any("你好完成" in text for text in output)
    assert sum("本次输出未采用" in text for text in output) == 1
    assert output[0] == "background update"


def test_projection_confirmed_body_rejects_late_delta_and_is_read_only():
    from agent_harness.frontends.conversation import ConversationProjection
    projection = ConversationProjection()
    projection.restore([], "test", 1)
    projection.apply_record(record(1, "assistant_message", {"model_call_id": "one", "text": "正式正文"}))
    assert projection.apply_delta(event("assistant_text_delta", {
        "model_call_id": "one", "block_index": 0, "sequence": 1, "delta": "过期正文"})) == []
    records = projection.records
    records[0]["payload"]["text"] = "外部修改"
    assert projection.records[0]["payload"]["text"] == "正式正文"
    projection.apply_record(record(2, "execution_status", {
        "phase": "model", "name": "请求模型", "state": "failed", "operation_id": "op", "model_call_id": "two"}))
    assert projection.apply_delta(event("assistant_text_delta", {
        "model_call_id": "two", "block_index": 0, "sequence": 1, "delta": "结束后迟到"})) == []


def test_projection_deduplicates_and_discards_only_matching_unaccepted_source():
    from agent_harness.frontends.conversation import ConversationProjection
    projection = ConversationProjection()
    delta = event("assistant_text_delta", {"model_call_id": "same", "block_index": 0, "sequence": 1, "delta": "Lead"})
    projection.apply_delta(delta)
    assert projection.apply_delta(delta) == []
    delta.source = "child"
    delta.payload = dict(delta.payload, delta="Child")
    projection.apply_delta(delta)
    changes = projection.apply_record(record(1, "execution_status", {
        "phase": "model", "name": "请求模型", "state": "failed", "operation_id": "op", "model_call_id": "same"}))
    assert [c["entry"]["text"] for c in changes if c["kind"] == "discarded"] == ["Lead"]
    delta.generation = 0
    assert projection.apply_delta(delta) == []
    assert next(e for e in projection.entries if e.get("source") == "child")["discarded"] is False


def test_tui_window_rebuild_preserves_expansion_and_live_body():
    from agent_harness.frontends.tui.app import HarnessApp
    from agent_harness.frontends.tui.widgets import OperationEntry, StreamingText
    from textual.widgets import Collapsible, Markdown
    app = HarnessApp(DisplayRuntime())

    async def exercise():
        async with app.run_test(size=(85, 30)) as pilot:
            await app.conversation.display_record(record(1, "tool_call", {
                "id": "tool", "name": "read_file", "input": {"path": "说明.md"}, "model_call_id": "model"}))
            app.query_one(OperationEntry).query_one(Collapsible).collapsed = False
            await app._event(event("assistant_text_delta", {
                "model_call_id": "next", "block_index": 0, "sequence": 1, "delta": "活动正文"}))
            await app.conversation.load_earlier()
            await pilot.pause()
            assert not app.query_one(OperationEntry).query_one(Collapsible).collapsed
            assert app.query_one(StreamingText).query_one(Markdown)._markdown == "活动正文"
            await app.conversation.display_record(record(2, "assistant_message", {
                "model_call_id": "next", "block_index": 0, "text": "正式正文"}))
            await app.conversation.load_earlier()
            await pilot.pause()
            assert app.query_one(StreamingText).query_one(Markdown)._markdown == "正式正文"
            failed = event("assistant_text_delta", {
                "model_call_id": "failed", "block_index": 0, "sequence": 1, "delta": "未采用正文"})
            await app._event(failed)
            await app.conversation.display_record(record(3, "execution_status", {
                "phase": "model", "name": "请求模型", "state": "failed", "operation_id": "failed", "model_call_id": "failed"}))
            streams = list(app.query(StreamingText))
            streams[-1].query_one(Collapsible).collapsed = False
            await app.conversation.load_earlier()
            await pilot.pause()
            assert not list(app.query(StreamingText))[-1].query_one(Collapsible).collapsed
    asyncio.run(exercise())
