"""Textual 对话控件与历史窗口的唯一拥有者。"""

from datetime import datetime, timezone

from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible

from ..conversation import ConversationProjection
from ..presentation import arguments_text, format_elapsed, session_title
from .widgets import (ApprovalCard, BrandBar, ConversationRecord, MessageRow, OperationEntry, ProcessGroup,
                      StreamingText)


def operation_text(entry):
    """Full operation text for the call frame: tool name plus complete arguments."""
    arguments = entry.get("arguments") or {}
    detail = arguments_text(arguments) if arguments else entry.get("output", "")
    return " · ".join(filter(None, (entry.get("tool_name"), detail)))


class ConversationView:
    def __init__(self, app):
        self.app = app
        self.projection = ConversationProjection()
        self.start = 0
        self.pending_inputs = {}
        self._operations = {}
        self._groups = {}
        self._streams = {}
        self._rows = {}

    @property
    def records(self):
        return self.projection.records

    def refresh_elapsed(self):
        now = datetime.now(timezone.utc)
        for key, entry in self._operations.items():
            entry.refresh_elapsed(now)
            group = self._groups.get(key[:3]) if isinstance(key, tuple) else None
            if group is not None and group.is_mounted:
                group.update_operation(key, operation_text(entry.entry), entry.entry["state"],
                                       format_elapsed(entry.elapsed_seconds(now)))

    async def _render_stream(self, entry):
        key = entry["key"]
        widget = self._streams.get(key)
        if widget is None or not widget.is_mounted:
            widget = StreamingText()
            self._streams[key] = widget
            await self.app.query_one("#chat", VerticalScroll).mount(widget)
        widget.render_entry(entry)

    async def _mount_record(self, record, entry):
        chat = self.app.query_one("#chat", VerticalScroll)
        data, kind = record["payload"], record["type"]
        index = next(i for i, item in enumerate(self.records) if item["id"] == record["id"])
        if kind == "assistant_message" and data.get("model_call_id"):
            stream = next((item for item in self.projection.entries if item["kind"] == "stream" and
                item["sequence"] > 0 and item["key"][2:] == (record.get("turn_id"), record["source"], data["model_call_id"], data.get("block_index", 0))), None)
            if stream is not None:
                await self._render_stream(stream)
                return
        if kind == "execution_status" or (kind in {"tool_call", "tool_result"} and data.get("model_call_id")):
            key = entry["key"]
            widget = self._operations.get(key)
            if widget is None or not widget.is_mounted:
                parent = chat
                group = None
                if data.get("model_call_id") and data.get("phase") != "model":
                    group_key = (record.get("turn_id"), record["source"], data["model_call_id"])
                    group = self._groups.get(group_key)
                    if group is None or not group.is_mounted:
                        group = ProcessGroup(record["source"])
                        group.record_start_index = index
                        self._groups[group_key] = group
                        await chat.mount(group)
                    parent = group.query_one(VerticalScroll)
                widget = OperationEntry(entry)
                widget.record_start_index = index
                self._operations[key] = widget
                await parent.mount(widget)
            widget.render_entry(entry)
            group = self._groups.get((record.get("turn_id"), record["source"], data.get("model_call_id")))
            if group is not None and group.is_mounted:
                group.update_operation(key, operation_text(entry), entry["state"],
                                       format_elapsed(widget.elapsed_seconds(datetime.now(timezone.utc))))
        else:
            # 每条记录独占一行：用户消息靠右，模型回复和事件靠左。
            row = MessageRow(classes="user" if kind == "user_message" else "")
            row.record_start_index = index
            self._rows[record["id"]] = row
            await chat.mount(row)
            await row.mount(ConversationRecord(record, entry["tool_name"]))

    async def text_delta(self, event):
        chat = self.app.query_one("#chat", VerticalScroll)
        follow = chat.scroll_y >= chat.max_scroll_y - 1 and self.app.approval is None
        for change in self.projection.apply_delta(event):
            await self._render_stream(change["entry"])
        if follow:
            chat.scroll_end(animate=False)

    async def display_record(self, record, *, session_id=None, generation=None):
        changes = self.projection.apply_record(record, session_id=session_id, generation=generation)
        if not changes:
            return
        self.app.query_one(BrandBar).update_title(session_title(self.app.snapshot, self.records))
        chat = self.app.query_one("#chat", VerticalScroll)
        follow = chat.scroll_y >= chat.max_scroll_y - 1 and self.app.approval is None
        pending = self.pending_inputs.pop(record["payload"].get("submission_id"), None) if record["type"] == "user_message" else None
        if pending is not None:
            pending.record = record
            pending.record_start_index = len(self.records) - 1
            self._rows[record["id"]] = pending
        for change in changes:
            if change["kind"] == "record" and pending is None:
                await self._mount_record(change["record"], change["entry"])
            elif change["kind"] in {"confirmed", "discarded"}:
                await self._render_stream(change["entry"])
        if record["type"] == "approval_decided":
            for card in self.app.query(ApprovalCard):
                if card.request_id == record["payload"]["request_id"]:
                    card.finish(record["payload"]["decision"])
        while len(chat.children) > 80 and self.app.approval is None and not self.app.busy:
            first = chat.children[0]
            decided = {item["payload"]["request_id"] for item in self.records if item["type"] == "approval_decided"}
            if any(not card.historical and card.request_id not in decided for card in first.query(ApprovalCard)):
                break
            await first.remove()
            # 一次移除整个控件组，窗口的下界不截断同次模型调用。
            if hasattr(first, "record_start_index"):
                self.start = max(self.start, first.record_start_index + 1)
        self.app.query_one("#earlier", Button).display = self.start > 0
        if follow:
            chat.scroll_end(animate=False)

    async def restore(self, records, session_id=None, generation=0):
        self.pending_inputs.clear()
        self.projection.restore(records, session_id, generation)
        self.start = max(0, len(self.records) - 80)
        await self.render_window(preserve=False)

    async def render_window(self, preserve=True):
        chat = self.app.query_one("#chat", VerticalScroll)
        widgets = dict(self._rows)
        widgets.update(self._operations)
        widgets.update(self._streams)
        # 展开状态属于视图，不参与语义投影或会话持久化。
        expanded = {key for key, widget in widgets.items() if widget.is_mounted and
                    any(not detail.collapsed for detail in widget.query(Collapsible))} if preserve else set()
        group_scroll = {key: group.query_one(VerticalScroll).scroll_y for key, group in self._groups.items()
                        if preserve and group.is_mounted}
        await chat.remove_children()
        self._streams.clear()
        self._operations.clear()
        self._groups.clear()
        self._rows.clear()
        records = self.records
        visible = records[self.start:]
        visible_ids = {r["id"] for r in visible}
        group_ids = {(r.get("turn_id"), r["source"], r["payload"].get("model_call_id")) for r in visible if r["payload"].get("model_call_id")}
        for record in records:
            group_key = (record.get("turn_id"), record["source"], record["payload"].get("model_call_id"))
            if record["id"] in visible_ids or group_key in group_ids or (record["type"] == "approval_requested" and
                    self.app.approval is not None and record["payload"]["request_id"] == self.app.approval.request_id):
                await self._mount_record(record, self.projection.entry_for(record))
        # 临时正文直接从投影恢复，控件不用反向提供状态。
        for stream in self.projection.entries:
            if stream["kind"] == "stream" and not stream["confirmed"]:
                await self._render_stream(stream)
        widgets = dict(self._rows)
        widgets.update(self._operations)
        widgets.update(self._streams)
        for key in expanded:
            if key in widgets:
                for detail in widgets[key].query(Collapsible):
                    detail.collapsed = False
        for key, y in group_scroll.items():
            if key in self._groups:
                scroll = self._groups[key].query_one(VerticalScroll)
                self.app.call_after_refresh(scroll.scroll_to, y=y, animate=False)
        decisions = {r["payload"]["request_id"]: r["payload"]["decision"] for r in records if r["type"] == "approval_decided"}
        for card in self.app.query(ApprovalCard):
            if card.request_id in decisions:
                card.finish(decisions[card.request_id])
            elif self.app.approval is not None and card.request_id == self.app.approval.request_id:
                card.enable()
        self.app.query_one("#earlier", Button).display = self.start > 0
        chat.scroll_end(animate=False)

    async def load_earlier(self):
        self.start = max(0, self.start - 80)
        await self.render_window()
        self.app.query_one("#chat", VerticalScroll).scroll_home(animate=False)

    async def enable_approval(self, request):
        card = next((card for card in self.app.query(ApprovalCard) if card.request_id == request.request_id), None)
        if card is None:
            index = next((i for i, record in enumerate(self.records) if record["type"] == "approval_requested" and
                          record["payload"]["request_id"] == request.request_id), None)
            if index is not None:
                self.start = min(self.start, index)
                await self.render_window()
                card = next(card for card in self.app.query(ApprovalCard) if card.request_id == request.request_id)
        if card is not None:
            card.enable()
            chat = self.app.query_one("#chat", VerticalScroll)
            chat.scroll_to_widget(card, animate=False)
            chat.scroll_end(animate=False)
