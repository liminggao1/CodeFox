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
    assert "read_file · 已返回结果" in text and "x" * 500 in text
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
            await app._display_record(record(1, "approval_requested", {"request_id": "old", "tool_name": "bash",
                                      "arguments": {}, "historical": True, "interrupted": True}))
            await app._display_record(record(2, "approval_requested", {"request_id": "current", "tool_name": "bash",
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
