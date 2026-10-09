"""Presentation-only dialogs for approvals and saved sessions."""

from __future__ import annotations

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, OptionList, Static
from textual.widgets.option_list import Option


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


class ApprovalScreen(ModalScreen[str]):
    BINDINGS = [("escape", "deny", "拒绝")]
    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; background: $background 70%; }
    ApprovalScreen > Vertical { width: 72; max-width: 95%; height: auto;
        max-height: 90%; border: solid $warning; padding: 1 2; background: $surface; }
    ApprovalScreen VerticalScroll { height: auto; max-height: 18; margin: 1 0; }
    ApprovalScreen Horizontal { height: 3; align-horizontal: right; }
    ApprovalScreen Button { margin-left: 1; }
    """

    def __init__(self, request):
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"工具审批 · {self.request.source} · {self.request.tool_name}")
            with VerticalScroll():
                yield Static(json.dumps(self.request.arguments, ensure_ascii=False, indent=2), markup=False)
            with Horizontal():
                yield Button("拒绝", id="deny")
                yield Button("批准", variant="warning", id="approve")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id)

    def action_deny(self):
        self.dismiss("deny")


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
