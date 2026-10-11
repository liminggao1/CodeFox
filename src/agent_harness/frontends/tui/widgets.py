"""Presentation-only conversation records and saved-session selection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.message import Message
from textual.widgets import Button, Collapsible, Label, ListItem, ListView, Markdown, OptionList, Static
from textual.widgets.option_list import Option
from ..presentation import (STATUS_LABELS, arguments_summary, arguments_text, format_elapsed,
                            format_session_state, full_text, lifecycle_summary, record_time, short_text,
                            todo_lines)


LOGO_PATH = Path(__file__).resolve().parents[4] / "assets" / "logo" / "1F98A.svg"


class DetailScroll(VerticalScroll):
    DEFAULT_CSS = """
    DetailScroll { height: auto; max-height: 10; }
    DetailScroll Static { height: auto; }
    """


class GroupHead(Static):
    """Clickable one-line header for a model call's operation frame."""

    class Toggle(Message):
        pass

    DEFAULT_CSS = """
    GroupHead { height: 1; color: #999fa5; }
    """

    def on_click(self):
        self.post_message(self.Toggle())


class BrandBar(Vertical):
    """Compact CodeFox identity and navigation actions.

    宽屏占 1 行：头像、会话标题和三个入口；窄屏第二行放会话标题和 ``[状态]`` 入口。
    """
    DEFAULT_CSS = """
    BrandBar { height: 1; padding: 0 1; background: #101214; }
    BrandBar #brand-row { height: 1; }
    BrandBar #brand-title-row { height: 1; display: none; }
    BrandBar .brand { width: 12; color: #80cbc4; text-style: bold; }
    BrandBar .session-title { width: 1fr; color: #e1e3e5; }
    BrandBar #brand-state-toggle { width: auto; min-width: 6; height: 1; display: none; }
    BrandBar #top-actions { width: auto; height: 1; align-horizontal: right; }
    BrandBar #top-actions Button { min-width: 6; height: 1; min-height: 1; border: none;
        margin-left: 1; padding: 0 1; }
    BrandBar #top-actions #new-session { color: #101214; background: #80cbc4; }
    """

    def compose(self) -> ComposeResult:
        # Logo 用终端字符表现；SVG 缺失时退回可稳定显示的狐狸字符，不加装饰符号。
        logo = "🦊" if LOGO_PATH.is_file() else "狐"
        with Horizontal(id="brand-row"):
            yield Static(f"{logo} CodeFox", classes="brand", markup=False)
            yield Static("未开始会话", classes="session-title", markup=False)
            with Horizontal(id="top-actions"):
                yield Button("历史", id="history")
                yield Button("设置", id="settings")
                yield Button("新会话", id="new-session", variant="primary")
        with Horizontal(id="brand-title-row"):
            yield Static("未开始会话", classes="session-title", markup=False)
            yield StateToggle("[状态]", id="brand-state-toggle", markup=False)

    def update_title(self, title):
        for widget in self.query(".session-title"):
            widget.update(title)


class StateToggle(Static):
    """One-line entry point that expands the collapsed read-only state panel."""

    class Pressed(Message):
        pass

    DEFAULT_CSS = """
    StateToggle { height: 1; color: #80cbc4; padding: 0 1; }
    """

    def on_click(self):
        self.post_message(self.Pressed())


class ProcessGroup(Vertical):
    """One model call's operations behind a collapsed frame.

    收起时显示正在进行的操作 ``运行中 <本次操作>``（多条时标注“还有 N 条进行中”）；
    没有进行中操作时标题回到 ``已运行的命令``，展开后列出各 ``已运行 <本次操作>`` 行。
    操作内容原样透传，不截断。
    """
    DEFAULT_CSS = """
    ProcessGroup { height: auto; margin: 1 0; background: #141618; border: solid #2c3033; padding: 0 1; }
    ProcessGroup GroupHead { height: 1; color: #999fa5; }
    ProcessGroup > VerticalScroll { height: auto; max-height: 14; }
    """

    RUNNING_STATES = {"pending", "running", "working", "postprocessing"}

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.expanded = False
        self.operations = {}

    def compose(self):
        yield GroupHead("已运行的命令", id="group-head")
        yield VerticalScroll(classes="process-content")

    def on_mount(self):
        self.query_one(VerticalScroll).display = False
        # 定时刷新可能在 compose 完成前记录过操作，这里补一次标题渲染。
        self._render_head()

    def update_operation(self, key, text, state, elapsed=None):
        """Record one operation's full text and refresh the header line."""
        self.operations[key] = (text, state, elapsed)
        self._render_head()

    def _render_head(self):
        # 挂载与 compose 之间存在窗口：容器已挂载但子控件尚未就绪时，
        # 定时刷新会在这里到达，先取一次实际存在的标题行。
        heads = self.query(GroupHead)
        if not heads:
            return
        head = heads.first()
        running = [value for value in self.operations.values() if value[1] in self.RUNNING_STATES]
        if running and not self.expanded:
            text, _state, elapsed = running[0]
            line = f"运行中 {text}"
            if elapsed:
                line += f" · 已耗时 {elapsed}"
            if len(running) > 1:
                line += f" · 还有 {len(running) - 1} 条进行中"
            head.update(line)
        else:
            head.update("已运行的命令")

    def on_group_head_toggle(self, event):
        self.expanded = not self.expanded
        content = self.query(VerticalScroll)
        if content:
            content.first().display = self.expanded
        self._render_head()


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
            arguments = entry["arguments"] or {}
            detail = arguments_text(arguments) if arguments else short_text(entry["output"]) if entry["output"] else ""
            summary = " · ".join(filter(None, ("已运行", entry["tool_name"], detail,
                                               STATUS_LABELS.get(entry["state"], entry["state"]))))
        # 进行中事件首次显示就带耗时；计时器随后只刷新这一行。
        self.query_one(".operation-summary", Static).update(
            self._summary_text(summary, datetime.now(timezone.utc)))
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
        # 窗口重建时，容器可能仍挂载但其子控件已开始卸载。
        if not self.is_mounted or not self.query(".operation-summary"):
            return
        summary = self.query_one(".operation-summary", Static).content
        if " · 耗时 " not in str(summary):
            return
        base = str(summary).rsplit(" · 耗时 ", 1)[0]
        self.query_one(".operation-summary", Static).update(self._summary_text(base, now))


class StreamingText(Vertical):
    DEFAULT_CSS = "StreamingText { height: auto; } StreamingText Static { height: auto; color: #999fa5; }"

    def compose(self):
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


class MessageRow(Horizontal):
    """One conversation record's row; user messages hug the right edge."""
    DEFAULT_CSS = """
    MessageRow { height: auto; }
    MessageRow.user { align-horizontal: right; }
    """


class ConversationRecord(Vertical):
    DEFAULT_CSS = """
    ConversationRecord { height: auto; margin-bottom: 1; }
    ConversationRecord.user-message { width: 82%; background: #202326; padding: 0 1; }
    ConversationRecord.model-message { width: 82%; background: #191b1d; padding: 0 1; }
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
            text = "\n".join(todo_lines(data.get("todos", []))) or "无待办"
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
    SessionScreen Horizontal { height: 3; align-horizontal: left; }
    SessionScreen #sessions-head { height: 1; align-horizontal: right; }
    SessionScreen #sessions-head Label { width: 1fr; color: #80cbc4; text-style: bold; }
    SessionScreen Button { margin-left: 1; }
    """

    def __init__(self, entries, active_id=None):
        super().__init__()
        self.entries = entries
        self.active_id = active_id

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="sessions-head"):
                yield Label("会话历史")
                yield Button("返回", id="cancel")
            yield Static("" if self.entries else "暂无保存的会话", markup=False)
            with ListView(id="sessions"):
                for entry in self.entries:
                    yield ListItem(Label(self._label(entry), markup=False))
            with Horizontal():
                yield Button("恢复所选", variant="primary", id="resume", disabled=not self.entries)
                yield Button("删除所选", variant="error", id="delete", disabled=not self.entries)
                yield Button("刷新", id="refresh")

    def _label(self, entry):
        """列表行文字：当前会话标注占用，读不出来的文件标注无法读取。"""
        if entry["id"] == self.active_id:
            return (f"{entry['id']}（当前会话 · 正在占用）\n"
                    f"{entry['updated_at']} · {entry['message_count']} 条消息\n{entry['summary']}")
        if entry.get("invalid"):
            return f"{entry['id']}（无法读取）\n{entry['summary']}"
        return f"{entry['id']}\n{entry['updated_at']} · {entry['message_count']} 条消息\n{entry['summary']}"

    def _selected(self):
        """当前高亮的会话条目；没有选中返回 None。"""
        index = self.query_one(ListView).index
        if index is None or not self.entries:
            return None
        return self.entries[index]

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "refresh":
            self.dismiss("/resume")
            return
        entry = self._selected()
        if entry is None:
            return
        if event.button.id == "delete" and entry["id"] == self.active_id:
            self.app.notify("当前会话正在使用，不能删除", severity="warning")
            return
        if event.button.id == "resume" and entry.get("invalid"):
            self.app.notify("该会话文件无法读取，不能恢复", severity="warning")
            return
        self.dismiss(f"/{event.button.id} {entry['id']}")

    def action_cancel(self):
        self.dismiss(None)


class SessionStatePanel(VerticalScroll):
    """Read-only projection of the active session state."""
    DEFAULT_CSS = """
    SessionStatePanel { width: 32; min-width: 26; padding: 0 1; background: #191b1d; border-left: solid #35393d; }
    SessionStatePanel #state-head { height: 1; }
    SessionStatePanel .state-title { width: 1fr; color: #80cbc4; text-style: bold; height: 1; }
    SessionStatePanel #state-collapse { min-width: 6; height: 1; min-height: 1; border: none; padding: 0 1; }
    SessionStatePanel .state-body { color: #b8bdc2; height: auto; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="state-head"):
            yield Static("会话状态", classes="state-title", markup=False)
            yield Button("收起", id="state-collapse")
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
