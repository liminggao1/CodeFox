"""Presentation-only conversation records and saved-session selection."""

from __future__ import annotations

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.message import Message
from textual.widgets import Button, Collapsible, Label, ListItem, ListView, Markdown, OptionList, Static
from textual.widgets.option_list import Option
from ..presentation import STATUS_LABELS, arguments_summary, full_text, lifecycle_summary, short_text


class DetailScroll(VerticalScroll):
    DEFAULT_CSS = """
    DetailScroll { height: auto; max-height: 10; }
    DetailScroll Static { height: auto; }
    """


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

    def __init__(self, record):
        super().__init__()
        self.record = record
        self.tool_name = record["payload"].get("name", "工具")
        self.arguments = record["payload"].get("input", {})
        self.state = "pending"
        self.output = ""

    def compose(self):
        yield Static(classes="operation-summary", markup=False)
        yield Static(classes="operation-preview", markup=False)
        yield Collapsible(DetailScroll(Static(classes="operation-detail", markup=False)),
                          title="详情", collapsed=True)

    def apply(self, record):
        self.record = record
        data, kind = record["payload"], record["type"]
        if kind == "tool_call":
            self.tool_name, self.arguments = data["name"], data["input"]
            if data.get("historical") and data.get("result_missing"):
                self.state = "interrupted"
        elif kind == "tool_result":
            self.state = data.get("status", "returned")
            self.output = full_text(data.get("content", ""))
        else:
            self.state = data["state"]
            if data.get("historical") and self.state == "running":
                self.state = "interrupted"
                data = dict(data, state="interrupted", reason="上次中断，结束状态未记录")
            self.output = data.get("reason", "")
        if kind == "execution_status" and data["phase"] != "tool":
            summary = lifecycle_summary(data)
        else:
            summary = f"工具 · {self.tool_name} · {arguments_summary(self.arguments)} · {STATUS_LABELS.get(self.state, self.state)}"
        self.query_one(".operation-summary", Static).update(summary)
        preview = self.query_one(".operation-preview", Static)
        preview.update(short_text(self.output) if self.output else "")
        preview.display = bool(self.output)
        self.query_one(".operation-detail", Static).update(full_text(self.arguments) + "\n" + self.output)


class StreamingText(Vertical):
    DEFAULT_CSS = "StreamingText { height: auto; } StreamingText Static { height: auto; color: #999fa5; }"

    def __init__(self):
        super().__init__()
        self.text = ""
        self.sequence = 0
        self.confirmed = False
        self.discarded = False

    def compose(self):
        yield Static("Lead", markup=False)
        yield Collapsible(Markdown(""), title="本次输出未采用", collapsed=False)

    def on_mount(self):
        self.query_one("CollapsibleTitle").display = False

    def append(self, data):
        if self.confirmed or data["sequence"] <= self.sequence:
            return
        self.sequence = data["sequence"]
        self.text += data["delta"]
        self.query_one(Markdown).update(self.text)

    def confirm(self, text):
        self.confirmed = True
        self.text = text
        self.query_one(Markdown).update(text)

    def discard(self):
        self.confirmed = True
        self.discarded = True
        detail = self.query_one(Collapsible)
        self.query_one("CollapsibleTitle").display = True
        detail.collapsed = True


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
    ConversationRecord .record-source { height: 1; color: #80cbc4; }
    ConversationRecord Static { height: auto; }
    ConversationRecord Collapsible { height: auto; padding: 0; background: transparent; }
    """

    def __init__(self, record, tool_name=None):
        super().__init__(id="record-" + record["id"])
        self.record = record
        self.tool_name = tool_name

    def compose(self) -> ComposeResult:
        record, data = self.record, self.record["payload"]
        kind, source = record["type"], record["source"]
        if kind in {"user_message", "assistant_message"}:
            yield Static("你" if kind == "user_message" else source, classes="record-source", markup=False)
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
