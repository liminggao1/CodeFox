"""Textual 对话控件与历史窗口的唯一拥有者。"""

from datetime import datetime, timezone

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, Static

from ..conversation import ConversationProjection
from ..presentation import arguments_text, format_elapsed, session_title
from .widgets import (ApprovalCard, BrandBar, ConversationRecord, DetailScroll, MessageRow, OperationEntry, ProcessGroup,
                      StreamingText)

# 只有进行中的状态才占用对话区一行；终态一律清掉，避免残留。
ACTIVE_STATUSES = frozenset({"处理中"})


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
        self._pending_inputs = {}
        self._pending_rows = {}
        self._operations = {}
        self._groups = {}
        self._streams = {}
        self._rows = {}
        self._activity_label = None
        self._activity = None
        self._diagnostics = []
        self.diagnostics_visible = True
        self._diagnostic_block = None
        self._diagnostic_detail = None
        self._errors = []

    @property
    def records(self):
        return self.projection.records

    def scroll_key(self, key):
        chat = self.app.query_one("#chat", VerticalScroll)
        if self.app.focused is not chat and not chat.has_focus:
            return False
        chat.scroll_y = 0 if key == "home" else chat.max_scroll_y
        return True

    def show_pending_input(self, submission_id, text):
        record = {"id": submission_id, "type": "user_message", "source": "lead",
                  "payload": {"text": text.strip()}}
        self._pending_inputs[submission_id] = record
        self._mount_pending(record)

    def _mount_pending(self, record):
        row = MessageRow(ConversationRecord(record), classes="user")
        self._pending_rows[record["id"]] = row
        return self.app.query_one("#chat", VerticalScroll).mount(row)

    def show_activity(self, label):
        """终态不留对话区行；结果和状态由记录行或底部状态行承担。"""
        self._activity_label = label if label in ACTIVE_STATUSES else None
        if self._activity_label is None:
            if self._activity is not None and self._activity.parent is not None:
                self._activity.remove()
            self._activity = None
        elif self._activity is None or self._activity.parent is None:
            self._activity = Static(label, classes="activity", markup=False)
            self.app.query_one("#chat", VerticalScroll).mount(self._activity)
        else:
            self._activity.update(label)

    def log(self, text):
        self._diagnostics.append(Text.from_ansi(str(text)).plain)
        self._diagnostics = self._diagnostics[-1000:]
        self._render_diagnostics()

    def set_diagnostics_visible(self, visible):
        """只切换显示，当前会话的日志继续按原有上限收集。"""
        self.diagnostics_visible = visible
        if self._diagnostic_block is not None:
            self._diagnostic_block.display = visible

    def _render_diagnostics(self, collapsed=True):
        """诊断日志只有一块，窗口重建后重新挂到对话区末尾。"""
        content = "\n".join(self._diagnostics)[-8000:]
        if not content:
            return
        chat = self.app.query_one("#chat", VerticalScroll)
        if self._diagnostic_block is not None and self._diagnostic_block.parent is chat:
            self._diagnostic_detail.update(content)
            self._diagnostic_block.display = self.diagnostics_visible
            return
        self._diagnostic_detail = Static(content, markup=False)
        self._diagnostic_block = Collapsible(
            DetailScroll(self._diagnostic_detail), title="诊断日志",
            collapsed=collapsed, classes="diagnostic")
        self._diagnostic_block.display = self.diagnostics_visible
        return chat.mount(self._diagnostic_block)

    async def show_error(self, text):
        self._errors.append(text)
        await self.app.query_one("#chat", VerticalScroll).mount(
            Static(text, classes="system-error", markup=False))

    def disable_approval(self, request_id):
        for card in self.app.query(ApprovalCard):
            if card.request_id == request_id:
                card.disable()

    def finish_approval(self, request_id, decision):
        for card in self.app.query(ApprovalCard):
            if card.request_id == request_id:
                card.finish(decision)

    def refresh_elapsed(self):
        now = datetime.now(timezone.utc)
        for key, entry in self._operations.items():
            entry.refresh_elapsed(now)
            group = self._groups.get(key[:3]) if isinstance(key, tuple) else None
            if group is not None and group.is_mounted:
                group.update_operation(key, operation_text(entry.entry), entry.entry["state"],
                                       format_elapsed(entry.elapsed_seconds(now)),
                                       started_at=entry.entry["started_at"],
                                       finished_at=entry.entry["finished_at"])

    async def _render_stream(self, entry):
        key = entry["key"]
        widget = self._streams.get(key)
        if widget is None or not widget.is_mounted:
            widget = StreamingText()
            self._streams[key] = widget
            await self.app.query_one("#chat", VerticalScroll).mount(widget)
        widget.render_entry(entry)

    async def _mount_record(self, record, entry):
        data, kind = record["payload"], record["type"]
        # 审批结果已由审批卡就地展示，不再单独占一行。
        if kind == "approval_decided":
            return
        # 回合收尾统计只进入诊断日志，不在对话区重复显示 Lead 行。
        if kind == "turn_finished":
            return
        # 模型生命周期由诊断日志和终态统计承担；对话区只展示模型正文。
        if kind == "execution_status" and data.get("phase") == "model":
            return
        chat = self.app.query_one("#chat", VerticalScroll)
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
                                       format_elapsed(widget.elapsed_seconds(datetime.now(timezone.utc))),
                                       started_at=entry["started_at"], finished_at=entry["finished_at"])
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
        # 命令结果只弹窗提示，不占对话区行。
        if record["type"] == "command_result":
            return
        self.app.query_one(BrandBar).update_title(session_title(self.app.snapshot, self.records))
        chat = self.app.query_one("#chat", VerticalScroll)
        follow = chat.scroll_y >= chat.max_scroll_y - 1 and self.app.approval is None
        submission_id = record["payload"].get("submission_id") if record["type"] == "user_message" else None
        self._pending_inputs.pop(submission_id, None)
        pending = self._pending_rows.pop(submission_id, None)
        if pending is not None:
            pending.query_one(ConversationRecord).record = record
            pending.record_start_index = len(self.records) - 1
            self._rows[record["id"]] = pending
        for change in changes:
            if change["kind"] == "record" and pending is None:
                await self._mount_record(change["record"], change["entry"])
            elif change["kind"] in {"confirmed", "discarded"}:
                await self._render_stream(change["entry"])
        if record["type"] == "approval_decided":
            self.finish_approval(record["payload"]["request_id"], record["payload"]["decision"])
        # 历史窗口只裁剪记录行与操作组，临时提示不占记录额度。
        history = [child for child in chat.children if hasattr(child, "record_start_index")]
        while len(history) > 80 and self.app.approval is None and not self.app.busy:
            first = history.pop(0)
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
        self._pending_inputs.clear()
        self._activity_label = None
        self._diagnostics.clear()
        self._errors.clear()
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
        group_state = {key: (group.expanded, group.query_one(VerticalScroll).scroll_y)
                       for key, group in self._groups.items() if preserve and group.is_mounted}
        # 诊断日志不属于语义记录，重建前记下它的展开状态。
        diagnostics_collapsed = (self._diagnostic_block.collapsed
                                 if preserve and self._diagnostic_block is not None else True)
        await chat.remove_children()
        self._streams.clear()
        self._operations.clear()
        self._groups.clear()
        self._rows.clear()
        self._pending_rows.clear()
        self._activity = None
        self._diagnostic_block = None
        self._diagnostic_detail = None
        records = self.records
        visible = records[self.start:]
        visible_ids = {r["id"] for r in visible}
        group_ids = {(r.get("turn_id"), r["source"], r["payload"].get("model_call_id")) for r in visible if r["payload"].get("model_call_id")}
        for record in records:
            # 历史里的命令结果也不回放到对话区，只作为会话文件记录存在。
            if record["type"] == "command_result":
                continue
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
        for key, (is_expanded, y) in group_state.items():
            if key in self._groups:
                group = self._groups[key]
                group.set_expanded(is_expanded)
                scroll = group.query_one(VerticalScroll)
                self.app.call_after_refresh(scroll.scroll_to, y=y, animate=False)
        decisions = {r["payload"]["request_id"]: r["payload"]["decision"] for r in records if r["type"] == "approval_decided"}
        for card in self.app.query(ApprovalCard):
            if card.request_id in decisions:
                card.finish(decisions[card.request_id])
            elif self.app.approval is not None and card.request_id == self.app.approval.request_id:
                card.enable()
        # 临时显示只留当前会话内存，重建时与记录一起恢复，不进入语义投影。
        for text in self._errors:
            await chat.mount(Static(text, classes="system-error", markup=False))
        for record in self._pending_inputs.values():
            await self._mount_pending(record)
        if self._activity_label is not None:
            self.show_activity(self._activity_label)
        mounted = self._render_diagnostics(diagnostics_collapsed)
        if mounted is not None:
            await mounted
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
