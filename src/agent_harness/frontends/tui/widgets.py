"""Presentation-only conversation records and saved-session selection."""

from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.message import Message
from textual.widgets import Button, Collapsible, Label, ListItem, ListView, Markdown, OptionList, Static
from textual.widgets.option_list import Option
from ..presentation import (STATUS_LABELS, arguments_summary, format_elapsed, format_session_state,
                            full_text, lifecycle_summary, record_time, short_text)


LOGO_PATH = Path(__file__).resolve().parents[4] / "assets" / "logo" / "1F98A.svg"


class DetailScroll(VerticalScroll):
    DEFAULT_CSS = """
    DetailScroll { height: auto; max-height: 10; }
    DetailScroll Static { height: auto; }
    """


class BrandBar(Horizontal):
    """Compact CodeFox identity and navigation actions."""
    DEFAULT_CSS = """
    BrandBar { height: 3; padding: 0 1; background: #101214; }
    BrandBar .brand { width: 24; color: #80cbc4; text-style: bold; }
    BrandBar .session-title { width: 1fr; color: #e1e3e5; padding: 1 0; }
    BrandBar #top-actions { width: auto; height: 3; align-horizontal: right; }
    BrandBar #top-actions Button { min-width: 8; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        logo = "🦊" if LOGO_PATH.is_file() else ">_"
        yield Static(f"{logo} CodeFox >_", classes="brand", markup=False)
        yield Static("未开始会话", classes="session-title", markup=False)
        with Horizontal(id="top-actions"):
            yield Button("会话恢复", id="session")
            yield Button("历史", id="history")
            yield Button("状态", id="state")
            yield Button("设置", id="settings")
            yield Button("···", id="more")

    def update_title(self, title):
        self.query_one(".session-title", Static).update(title)


class ProcessGroup(Vertical):
    """One model call's operations, with bounded independent scrolling."""
    DEFAULT_CSS = """
    ProcessGroup { height: auto; margin: 1 0; }
    ProcessGroup > Static { height: 1; color: #999fa5; }
    ProcessGroup > VerticalScroll { height: auto; max-height: 14; }
    """

    def __init__(self, source):
        super().__init__()
        self.source = source

    def compose(self):
        yield Static(f"{self.source} · 操作", markup=False)
        yield VerticalScroll(classes="process-content")


class OperationEntry(Vertical):
    """Update one operation in place while preserving its expanded details."""
    DEFAULT_CSS = """
    OperationEntry { height: auto; }
    OperationEntry Static { height: auto; }
    OperationEntry Collapsible { height: auto; padding: 0; background: transparent; }
    OperationEntry .operation-preview { color: #999fa5; padding-left: 2; }
    """

    def __init__(self, entry):
        super().__init__()
        self.entry = entry

    def compose(self):
        yield Static(classes="operation-summary", markup=False)
        yield Static(classes="operation-preview", markup=False)
        yield Collapsible(DetailScroll(Static(classes="operation-detail", markup=False)),
                          title="详情", collapsed=True)

    def render_entry(self, entry):
        """状态由共享投影提供，控件只更新文字并保留展开状态。"""
        self.entry = entry
        record = entry["record"]
        data = dict(record["payload"], state=entry["state"])
        if record["type"] == "execution_status" and data["phase"] != "tool":
            summary = lifecycle_summary(data)
        else:
            summary = f"工具 · {entry['tool_name']} · {arguments_summary(entry['arguments'])} · {STATUS_LABELS.get(entry['state'], entry['state'])}"
        self.query_one(".operation-summary", Static).update(self._summary_text(summary))
        preview = self.query_one(".operation-preview", Static)
        preview.update(short_text(entry["output"]) if entry["output"] else "")
        preview.display = bool(entry["output"])
        self.query_one(".operation-detail", Static).update(full_text(entry["arguments"]) + "\n" + entry["output"])

    def _summary_text(self, summary, now=None):
        elapsed = self.elapsed_seconds(now)
        return summary + (f" · 耗时 {format_elapsed(elapsed)}" if elapsed is not None else "")

    def elapsed_seconds(self, now=None):
        if self.entry["elapsed_seconds"] is not None:
            return self.entry["elapsed_seconds"]
        started_at = record_time(self.entry["started_at"])
        if started_at is None:
            return None
        end = record_time(self.entry["finished_at"]) or now
        if end is None:
            return None
        return max(0.0, (end - started_at).total_seconds())

    def refresh_elapsed(self, now):
        if not self.is_mounted:
            return
        summary = self.query_one(".operation-summary", Static).content
        if " · 耗时 " not in str(summary):
            return
        base = str(summary).rsplit(" · 耗时 ", 1)[0]
        self.query_one(".operation-summary", Static).update(self._summary_text(base, now))


class StreamingText(Vertical):
    DEFAULT_CSS = "StreamingText { height: auto; } StreamingText Static { height: auto; color: #999fa5; }"

    def compose(self):
        yield Static("Lead", markup=False)
        yield Collapsible(Markdown(""), title="本次输出未采用", collapsed=False)

    def on_mount(self):
        self.query_one("CollapsibleTitle").display = False

    def render_entry(self, entry):
        self.query_one(Markdown).update(entry["text"])
        self.query_one("CollapsibleTitle").display = entry["discarded"]
        if entry["discarded"]:
            self.query_one(Collapsible).collapsed = True


class CommandCandidates(OptionList):
    """A display-only list that leaves keyboard focus in the editor."""

    can_focus = False
    DEFAULT_CSS = """
    CommandCandidates { display: none; height: 4; border: none; padding: 0;
        background: $surface; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.names = ()

    def show_commands(self, commands):
        names = tuple(command.name for command in commands)
        if names != self.names:
            self.names = names
            self.clear_options()
            self.add_options(Option(Text(f"{command.name}  {command.description}",
                                         no_wrap=True, overflow="ellipsis"), id=command.name)
                             for command in commands)
            self.highlighted = 0 if names else None
        self.display = bool(names)


class ApprovalCard(Vertical):
    DEFAULT_CSS = """
    ApprovalCard { height: auto; border-left: solid $warning; padding: 0 1; }
    ApprovalCard Horizontal { height: 3; }
    ApprovalCard Button { min-width: 8; margin-right: 1; }
    """

    class Choice(Message):
        def __init__(self, card, decision):
            super().__init__()
            self.card = card
            self.decision = decision

    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.request_id = payload["request_id"]
        self.historical = payload.get("historical", False)

    def compose(self) -> ComposeResult:
        yield Static("上次中断，未继续执行" if self.payload.get("interrupted") else
                     "历史审批" if self.historical else "等待审批", classes="approval-state", markup=False)
        yield Collapsible(Static(json.dumps(self.payload.get("arguments", {}), ensure_ascii=False, indent=2), markup=False),
                          title=self.payload["tool_name"], collapsed=True)
        if not self.historical:
            with Horizontal():
                yield Button("拒绝", name="deny", classes="deny", disabled=True)
                yield Button("批准", name="approve", classes="approve", variant="warning", disabled=True)

    def enable(self):
        if not self.historical:
            self.query_one(".approval-state", Static).update("等待你的决定")
            for button in self.query(Button):
                button.disabled = False

    def finish(self, decision):
        labels = {"approve": "已批准", "deny": "已拒绝", "closed": "已关闭"}
        self.query_one(".approval-state", Static).update(labels.get(decision, decision))
        self.disable()

    def disable(self):
        for button in self.query(Button):
            button.disabled = True

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        self.post_message(self.Choice(self, event.button.name))


class ConversationRecord(Vertical):
    DEFAULT_CSS = """
    ConversationRecord { height: auto; margin-bottom: 1; }
    ConversationRecord.user-message { width: 82%; margin-left: 10; background: #202326; padding: 0 1; }
    ConversationRecord.model-message { width: 82%; margin-right: 10; background: #191b1d; padding: 0 1; }
    ConversationRecord.event-record { width: 100%; background: #17191b; color: #999fa5; padding: 0 1; }
    ConversationRecord .record-source { height: 1; color: #80cbc4; }
    ConversationRecord Static { height: auto; }
    ConversationRecord Collapsible { height: auto; padding: 0; background: transparent; }
    """

    def __init__(self, record, tool_name=None):
        kind = record["type"]
        classes = "user-message" if kind == "user_message" else "model-message" if kind == "assistant_message" else "event-record"
        super().__init__(id="record-" + record["id"], classes=classes)
        self.record = record
        self.tool_name = tool_name

    def compose(self) -> ComposeResult:
        record, data = self.record, self.record["payload"]
        kind, source = record["type"], record["source"]
        if kind in {"user_message", "assistant_message"}:
            yield Static("你" if kind == "user_message" else "CodeFox" if source == "lead" else source,
                         classes="record-source", markup=False)
            yield Markdown(data["text"]) if kind == "assistant_message" else Static(data["text"], markup=False)
            return
        yield Static(source, classes="record-source", markup=False)
        if kind == "tool_call":
            yield Static(f"工具 · {data['name']} · {arguments_summary(data.get('input', {}))} · 待执行", markup=False)
            yield Collapsible(DetailScroll(Static(full_text(data.get("input", {})), markup=False)),
                              title="参数详情", collapsed=True)
            if data.get("result_missing"):
                yield Static("结果未记录，请核对当前状态", markup=False)
        elif kind == "tool_result":
            labels = {"returned": "已返回结果", "success": "成功", "failed": "失败", "denied": "已拒绝",
                      "stopped": "已停止", "started": "后台已启动"}
            status = labels.get(data.get("status"), "已返回结果")
            content = data.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            yield Static(f"{self.tool_name or '工具'} · {status}\n{short_text(content)}", markup=False)
            yield Collapsible(DetailScroll(Static(content, markup=False)), title="完整结果", collapsed=True)
        elif kind == "execution_status":
            yield Static(lifecycle_summary(data), markup=False)
            if data.get("reason"):
                yield Static(short_text(data["reason"]), markup=False)
        elif kind == "approval_requested":
            yield ApprovalCard(data)
        elif kind == "approval_decided":
            labels = {"approve": "已批准", "deny": "已拒绝", "closed": "已关闭"}
            yield Static(f"{data.get('tool_name', '工具')} · {labels.get(data['decision'], data['decision'])}", markup=False)
        elif kind == "todo_updated":
            labels = {"pending": "待办", "in_progress": "进行中", "completed": "完成"}
            text = "\n".join(f"{labels.get(item.get('status'), item.get('status', ''))} · {item.get('content', '')}"
                             for item in data.get("todos", [])) or "无待办"
            yield Static("TODO\n" + text, markup=False)
        elif kind in {"subagent_status", "background_status"}:
            name = data.get("name") or data.get("task_id", "后台任务")
            status = "上次中断，结束状态未记录" if data.get("interrupted") else data["status"]
            yield Static(f"{name} · {status}", markup=False)
        elif kind == "command_result":
            result = data.get("data", {})
            yield Static(str(result.get("text") or result.get("error") or data.get("command", "")), markup=False)
        elif kind == "turn_finished":
            outcome, summary = data.get("outcome", {}), data.get("summary", {})
            labels = {"completed": "完成", "failed": "失败", "stopped": "已停止", "truncated": "输出截断"}
            status = outcome.get("status", data.get("status", "completed"))
            text = labels.get(status, status)
            if summary:
                text += f" · {summary.get('elapsed_seconds', 0):.1f}s · 工具 {summary.get('tool_calls', 0)} · 审批 {summary.get('approvals', 0)}"
                usage = summary.get("lead_usage", {})
                text += f"\nLead 主模型 · 请求 {usage.get('model_calls', 0)} · 输入 {usage.get('input_tokens', 0)} · 输出 {usage.get('output_tokens', 0)}"
            if outcome.get("reason"):
                text += "\n" + outcome["reason"]
            yield Static(text, markup=False)


class SessionScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "返回")]
    DEFAULT_CSS = """
    SessionScreen { align: center middle; background: $background 70%; }
    SessionScreen > Vertical { width: 90; max-width: 95%; height: 80%;
        border: solid $primary; padding: 1 2; background: $surface; }
    SessionScreen ListView { height: 1fr; margin: 1 0; }
    SessionScreen ListItem { padding: 1; height: auto; }
    SessionScreen Horizontal { height: 3; align-horizontal: right; }
    SessionScreen Button { margin-left: 1; }
    """

    def __init__(self, entries, errors):
        super().__init__()
        self.entries = entries
        self.errors = errors

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("保存的会话")
            yield Static("\n".join(self.errors) if self.errors else ("" if self.entries else "暂无保存的会话"), markup=False)
            with ListView(id="sessions"):
                for entry in self.entries:
                    yield ListItem(Label(f"{entry['id']}\n{entry['updated_at']} · {entry['message_count']} 条消息\n{entry['summary']}", markup=False))
            with Horizontal():
                yield Button("返回", id="cancel")
                yield Button("删除", variant="error", id="delete", disabled=not self.entries)
                yield Button("恢复", variant="primary", id="resume", disabled=not self.entries)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        index = self.query_one(ListView).index
        if index is not None and self.entries:
            self.dismiss(f"/{event.button.id} {self.entries[index]['id']}")

    def on_list_view_selected(self, event: ListView.Selected):
        index = self.query_one(ListView).index
        if index is not None:
            self.dismiss(f"/resume {self.entries[index]['id']}")

    def action_cancel(self):
        self.dismiss(None)


class SessionStatePanel(VerticalScroll):
    """Read-only projection of the active session state."""
    DEFAULT_CSS = """
    SessionStatePanel { width: 32; min-width: 26; padding: 1 2; background: #191b1d; border-left: solid #35393d; }
    SessionStatePanel .state-title { color: #80cbc4; text-style: bold; height: 1; }
    SessionStatePanel .state-body { color: #b8bdc2; height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Static("会话状态", classes="state-title", markup=False)
        yield Static("未开始会话", classes="state-body", markup=False)

    def update_snapshot(self, snapshot, records=()):
        self.query_one(".state-body", Static).update(format_session_state(snapshot, records=records))


class SessionInfoScreen(ModalScreen[None]):
    BINDINGS = [("escape", "cancel", "返回")]
    DEFAULT_CSS = """
    SessionInfoScreen { align: center middle; background: $background 70%; }
    SessionInfoScreen > Vertical { width: 80; max-width: 95%; height: 80%; border: solid $primary;
        padding: 1 2; background: $surface; }
    SessionInfoScreen .info-body { height: 1fr; color: #c5c9cc; padding: 1 0; }
    SessionInfoScreen Horizontal { height: 3; align-horizontal: right; }
    """

    def __init__(self, snapshot, records=()):
        super().__init__()
        self.snapshot = snapshot
        self.records = records

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("运行信息")
            yield Static(format_session_state(self.snapshot, records=self.records), classes="info-body", markup=False)
            with Horizontal():
                yield Button("返回", id="cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)
