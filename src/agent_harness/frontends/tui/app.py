"""Textual adapter: UI state is a display copy, never business state."""

from __future__ import annotations

import json
import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Static, TextArea

from ... import core
from ...runtime.interactive_runtime import InteractiveRuntime
from ...runtime.local_commands import LOCAL_COMMANDS, find_command
from .widgets import (ApprovalCard, BrandBar, CommandCandidates, ConversationRecord, DetailScroll,
                      SessionInfoScreen, SessionScreen, SessionStatePanel, StateToggle)
from ..presentation import (PERMISSION_DEFAULT, PERMISSION_WAITING, message_count, model_status,
                            permission_summary, session_title)
from .conversation_view import ConversationView


class HarnessApp(App):
    TITLE = "Agent Harness"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("up", "command_up", show=False, priority=True),
        Binding("down", "command_down", show=False, priority=True),
        Binding("tab", "command_fill", show=False, priority=True),
        Binding("enter", "submit_input", show=False, priority=True),
        Binding("escape", "command_hide", show=False, priority=True),
        Binding("ctrl+enter", "send", "发送", priority=True),
        Binding("ctrl+l", "clear_input", "清空", priority=True),
        Binding("ctrl+r", "sessions", "会话", priority=True),
        Binding("ctrl+c", "stop_turn", "停止", priority=True),
        Binding("ctrl+d", "close_runtime", "退出", priority=True),
    ]
    CSS = """
    Screen { background: #151719; color: #e1e3e5; }
    #workspace { height: 1fr; }
    #conversation { width: 1fr; height: 1fr; padding: 0 1; }
    #conversation-top { height: 1; }
    #conversation-top #earlier { width: auto; min-width: 0; height: 1; min-height: 1; border: none; padding: 0 1; }
    #conversation-top #state-spacer { width: 1fr; }
    #conversation-top #state-toggle { width: auto; min-width: 6; height: 1; min-height: 1; }
    #chat { height: 1fr; }
    #earlier { display: none; }
    Markdown { padding: 0 1; margin: 0; background: transparent; }
    .activity { height: auto; color: #999fa5; margin: 1 0; }
    .diagnostic { height: auto; padding: 0; }
    #command-hint { display: none; height: 1; color: #80cbc4; }
    #input-area { height: auto; padding: 0 1; }
    #input-status { height: 1; color: #999fa5; }
    #input-box { height: 4; margin-top: 1; border: solid #55595d; background: #202326; }
    #input-row { height: 1; }
    #input { width: 1fr; height: 1; border: none; background: transparent; padding: 0 1; }
    #send { width: auto; min-width: 8; height: 1; min-height: 1; border: none; margin: 0 1; padding: 0 1; }
    #input-hint { height: 1; color: #999fa5; padding: 0 1; }
    #input-footer { height: 1; color: #999fa5; }
    #permission { width: 1fr; color: #b3a35a; }
    #bottom-status { width: auto; color: #999fa5; }
    .short #input-box { height: 3; margin-top: 0; }
    .short #input-hint { display: none; }
    .short #input-footer { display: none; }
    .short #input-status { display: none; }
    .short BrandBar { height: 1; }
    .short BrandBar .brand, .short BrandBar .session-title { height: 1; padding: 0; }
    .short BrandBar #top-actions Button { display: none; }
    .short #brand-title-row { display: none; }
    .short.narrow #brand-row .session-title { display: block; }
    .narrow BrandBar { height: 2; }
    .narrow #brand-title-row { display: block; }
    .narrow #brand-row .session-title { display: none; }
    .narrow #top-actions { width: 1fr; }
    .narrow #conversation { width: 1fr; }
    .narrow #session-status { width: 1fr; min-width: 0; }
    """

    def __init__(self, runtime=None):
        super().__init__()
        self.runtime = runtime or InteractiveRuntime()
        self.generation = 0
        self.sequence = 0
        self.turn_id = None
        self.busy = False
        self.stopping = False
        self.closing = False
        self.state_expanded = False
        self._notice = None
        self.runtime_closed = False
        self.approval = None
        self.snapshot = {}
        self.narrow = False
        self.short = False
        self._hidden_command_text = None
        self.conversation = ConversationView(self)
        self._consume_timer = None
        self._clock_timer = None
        self._activity = None
        self._diagnostics = []
        self._diagnostic_detail = None

    @property
    def records(self):
        return self.conversation.records

    def compose(self) -> ComposeResult:
        yield BrandBar()
        with Horizontal(id="workspace"):
            with Vertical(id="conversation"):
                with Horizontal(id="conversation-top"):
                    yield Button("加载更早记录", id="earlier")
                    yield Static(id="state-spacer")
                    yield StateToggle("[状态]", id="state-toggle", markup=False)
                yield VerticalScroll(id="chat")
                yield CommandCandidates(id="commands")
                yield Static(id="command-hint", markup=False)
            yield SessionStatePanel(id="session-status")
        # 输入区在主体之外：窄屏展开状态面板时对话列让位，输入框不被压缩。
        with Vertical(id="input-area"):
            yield Static("就绪", id="input-status", markup=False)
            with Vertical(id="input-box"):
                with Horizontal(id="input-row"):
                    yield TextArea(id="input", soft_wrap=True, placeholder="输入消息或 / 命令…")
                    yield Button("发送", id="send", variant="primary")
                yield Static("Enter 发送 · Shift+Enter 换行", id="input-hint", markup=False)
            with Horizontal(id="input-footer"):
                yield Static(permission_summary(PERMISSION_DEFAULT), id="permission", markup=False)
                yield Static(id="bottom-status", markup=False)

    def on_mount(self):
        self.runtime.start()
        self._consume_timer = self.set_interval(0.05, self.consume)
        self._clock_timer = self.set_interval(0.25, self.conversation.refresh_elapsed)
        self.query_one("#input", TextArea).focus()
        self._controls()

    def on_resize(self, event):
        self.short = event.size.height < 16
        self.narrow = event.size.width < 90
        self.set_class(self.short, "short")
        self.set_class(self.narrow, "narrow")
        self._controls()

    def on_key(self, event):
        """Keep Home/End deterministic when the conversation itself has focus."""
        if event.key == "shift+enter" and self.focused is self.query_one("#input", TextArea):
            self.query_one("#input", TextArea).insert("\n")
            event.stop()
            return
        if event.key not in {"home", "end"}:
            return
        chat = self.query_one("#chat", VerticalScroll)
        if self.focused is not chat and not chat.has_focus:
            return
        chat.scroll_y = 0 if event.key == "home" else chat.max_scroll_y
        event.stop()

    def _controls(self):
        button = self.query_one("#send", Button)
        if self.closing or self.stopping:
            button.label = "停止"
            button.disabled = True
        elif self.busy:
            button.label = "停止"
            button.disabled = self.turn_id is None
        else:
            button.label = "发送"
            button.disabled = False
        self.query_one("#bottom-status", Static).update(
            model_status(self.snapshot, message_count(self.snapshot, self.records), core.CONTEXT_LIMIT))
        self._input_status()
        self.query_one(SessionStatePanel).update_snapshot(self.snapshot, self.records)
        self.query_one(SessionStatePanel).display = self.state_expanded
        # 窄屏展开状态面板时对话列让位，面板占满主体宽度，输入区保持可见。
        self.query_one("#conversation").display = not (self.narrow and self.state_expanded)
        self.query_one("#state-toggle", StateToggle).display = not self.state_expanded and (self.short or not self.narrow)
        self.query_one("#brand-state-toggle", StateToggle).display = (
            self.narrow and not self.short and not self.state_expanded)
        self.query_one(BrandBar).update_title(session_title(self.snapshot, self.records))
        self._refresh_commands()

    def _set_permission(self, text):
        self.query_one("#permission", Static).update(permission_summary(text))

    # Command input decisions are local; the runtime remains the execution authority.
    def _command_context(self):
        if len(self.screen_stack) != 1 or self.busy or self.closing:
            return None
        editor = self.query_one("#input", TextArea)
        text = editor.text
        if (self.focused is not editor or "\n" in text or "\r" in text
                or not editor.selection.is_empty or editor.cursor_location != (0, len(text))):
            return None
        stripped = text.lstrip()
        name = stripped.split(maxsplit=1)[0] if stripped else ""
        if not re.fullmatch(r"/[a-z]*", name):
            return None
        return name, find_command(name), stripped == name

    def _refresh_commands(self):
        if not self.is_running:
            return
        editor = self.query_one("#input", TextArea)
        if editor.text != self._hidden_command_text:
            self._hidden_command_text = None
        panel = self.query_one(CommandCandidates)
        hint = self.query_one("#command-hint", Static)
        context = self._command_context()
        commands = ()
        message = ""
        if context and self._hidden_command_text is None:
            name, command, in_name = context
            if in_name:
                commands = tuple(item for item in LOCAL_COMMANDS if item.name.startswith(name))
                if not commands:
                    message = "没有匹配命令；Ctrl+Enter 可发送原文"
            elif command:
                message = command.usage
                if command.requires_argument and len(editor.text.split()) == 1:
                    message = "请输入会话 ID · " + command.usage
        panel.show_commands(commands)
        if commands:
            index = panel.highlighted if panel.highlighted is not None else 0
            message = commands[index].usage
        if hint.content != message:
            hint.update(message)
        hint.display = bool(message)
        reserved = 7 if self.size.height < 16 else 13
        panel.styles.height = max(1, min(len(commands), self.size.height - reserved))

    def on_text_area_changed(self, event: TextArea.Changed):
        self._refresh_commands()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged):
        self._refresh_commands()

    def on_descendant_focus(self, event):
        self._refresh_commands()

    def on_descendant_blur(self, event):
        self.call_after_refresh(self._refresh_commands)

    def check_action(self, action, parameters):
        if action == "submit_input" and len(self.screen_stack) != 1:
            return False
        if not action.startswith("command_"):
            return True
        self._refresh_commands()
        context = self._command_context()
        if context is None:
            return False
        panel = self.query_one(CommandCandidates)
        if action == "command_enter":
            return True
        if action == "command_hide":
            return panel.display or self.query_one("#command-hint").display
        return panel.display and bool(panel.names)

    def action_command_up(self):
        self.query_one(CommandCandidates).action_cursor_up()
        self._refresh_commands()

    def action_command_down(self):
        self.query_one(CommandCandidates).action_cursor_down()
        self._refresh_commands()

    def action_command_hide(self):
        self._hidden_command_text = self.query_one("#input", TextArea).text
        self._refresh_commands()

    def _choose_command(self, name, *, execute):
        self._refresh_commands()
        context = self._command_context()
        panel = self.query_one(CommandCandidates)
        if not context or not context[2] or name not in panel.names:
            return
        editor = self.query_one("#input", TextArea)
        command = find_command(name)
        prefix = editor.text[:len(editor.text) - len(editor.text.lstrip())]
        editor.load_text(prefix + name + (" " if command.requires_argument else ""))
        editor.move_cursor((0, len(editor.text)))
        if execute:
            self._send_draft()
        self._refresh_commands()

    def action_command_fill(self):
        panel = self.query_one(CommandCandidates)
        if panel.highlighted is not None:
            self._choose_command(panel.names[panel.highlighted], execute=False)

    def action_command_enter(self):
        self._refresh_commands()
        context = self._command_context()
        if context is None:
            return
        panel = self.query_one(CommandCandidates)
        if panel.display and panel.highlighted is not None:
            self._choose_command(panel.names[panel.highlighted], execute=True)
        elif context[1]:
            self._send_draft()
        else:
            self.notify("没有匹配命令；Ctrl+Enter 可发送原文", severity="warning")

    def on_option_list_option_selected(self, event: CommandCandidates.OptionSelected):
        if event.option_list.id == "commands":
            self._choose_command(event.option_id, execute=True)

    def _send_draft(self):
        editor = self.query_one("#input", TextArea)
        words = editor.text.split()
        command = find_command(words[0]) if words else None
        if command and command.requires_argument and len(words) == 1:
            editor.load_text(editor.text.rstrip() + " ")
            editor.move_cursor((0, len(editor.text)))
            self.notify("请输入会话 ID", severity="warning")
            self._refresh_commands()
            return
        if self._submit(editor.text):
            editor.clear()
            self._hidden_command_text = None
            self._refresh_commands()

    def _status(self, label="就绪"):
        self._notice = None if label == "就绪" else label
        self._input_status()
        if self._activity is None or self._activity.parent is None:
            if label == "就绪":
                return
            self._activity = Static(label, classes="activity", markup=False)
            self.query_one("#chat", VerticalScroll).mount(self._activity)
        else:
            self._activity.update(label)

    def _input_status(self):
        """Input-area status line: runtime state first, then the last notice."""
        if self.closing:
            label = "关闭中"
        elif self.busy:
            label = "处理中"
        else:
            label = self._notice or "就绪"
        self.query_one("#input-status", Static).update(label)

    def action_send(self):
        if len(self.screen_stack) != 1:
            return
        self._send_draft()

    def action_submit_input(self):
        """Enter sends ordinary text and retains the command completion rules."""
        if len(self.screen_stack) != 1:
            return
        context = self._command_context()
        if context is not None:
            self.action_command_enter()
        elif not self.busy:
            self._send_draft()

    def _submit(self, text):
        if not text or not text.strip():
            return False
        result = self.runtime.submit(text)
        if result.status != "accepted":
            self.notify(result.reason, severity="warning")
            return False
        self.busy = True
        if result.kind == "turn":
            pending = ConversationRecord({"id": result.submission_id, "type": "user_message",
                                          "source": "lead", "payload": {"text": text.strip()}})
            self.conversation.pending_inputs[result.submission_id] = pending
            self.query_one("#chat", VerticalScroll).mount(pending)
        self._activity = None
        self._diagnostic_detail = None
        self._controls()
        self._status("正在处理请求")
        return True

    def action_clear_input(self):
        if len(self.screen_stack) != 1:
            return
        self._hidden_command_text = None
        self.query_one("#input", TextArea).clear()
        self._refresh_commands()

    def action_sessions(self):
        if len(self.screen_stack) == 1:
            self._submit("/resume")

    def action_new_session(self):
        if len(self.screen_stack) == 1:
            self._submit("/new")

    def action_stop_turn(self):
        if self.turn_id and self.runtime.request_stop(self.turn_id):
            self.stopping = True
            self._controls()
            self._dismiss_approval()

    def action_close_runtime(self):
        self.closing = True
        self.stopping = False
        self.runtime.close()
        self._dismiss_approval()
        self._controls()
        self._status("正在关闭")

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "send":
            if self.busy:
                self.action_stop_turn()
            else:
                self.action_send()
        elif event.button.id == "earlier":
            await self.conversation.load_earlier()
        elif event.button.id == "state-collapse":
            self.state_expanded = False
            self._controls()
        elif event.button.id == "history":
            self.action_sessions()
        elif event.button.id == "settings":
            self.push_screen(SessionInfoScreen(self.snapshot, self.records))
        elif event.button.id == "new-session":
            self.action_new_session()
        self._focus_input()

    def on_state_toggle_pressed(self, event: StateToggle.Pressed):
        self.state_expanded = True
        self._controls()
        self._focus_input()

    def _focus_input(self):
        """顶部入口和状态切换不保留焦点，输入框始终可以继续编辑草稿。"""
        if self.is_running:
            self.query_one("#input", TextArea).focus()

    def _dismiss_approval(self):
        if self.approval is not None:
            for card in self.query(ApprovalCard):
                if card.request_id == self.approval.request_id:
                    card.disable()
            self.approval = None
        if self.is_running:
            self._set_permission(PERMISSION_DEFAULT)

    def on_approval_card_choice(self, event: ApprovalCard.Choice):
        request = self.approval
        if request is None or event.card.request_id != request.request_id:
            return
        if self.runtime.channel.respond(request.request_id, request.generation, event.decision):
            event.card.finish(event.decision)
            self.approval = None
            self._set_permission(PERMISSION_DEFAULT)
            self.query_one("#input", TextArea).focus()

    def _log(self, text):
        text = Text.from_ansi(str(text)).plain
        self._diagnostics.append(text)
        self._diagnostics = self._diagnostics[-1000:]
        content = "\n".join(self._diagnostics)[-8000:]
        if self._diagnostic_detail is None:
            self._diagnostic_detail = Static(content, markup=False)
            self.query_one("#chat", VerticalScroll).mount(Collapsible(
                DetailScroll(self._diagnostic_detail), title="诊断日志", collapsed=True, classes="diagnostic"))
        else:
            self._diagnostic_detail.update(content)

    async def consume(self):
        if not self.is_running:
            return
        # Drain a bounded batch so large output cannot starve UI input.
        batch = []
        for _ in range(100):
            event = self.runtime.channel.next_event()
            if event is None:
                break
            batch.append(event)
        index = 0
        while index < len(batch):
            event = batch[index]
            acknowledged = [event]
            index += 1
            if event.scope == "session":
                self.generation = event.generation
            self.sequence = event.sequence
            try:
                await self._event(event)
            except Exception:
                self.runtime.close()
                raise
            for original in acknowledged:
                self.runtime.channel.acknowledge_event(original.sequence)
            if not self.is_running:
                return
        if self.approval is None and not self.closing:
            request = self.runtime.channel.next_approval()
            if request is not None:
                self.approval = request
                self._set_permission(PERMISSION_WAITING)
                await self.conversation.enable_approval(request)
        self._refresh_commands()

    async def _event(self, event):
        data = event.payload
        kind = event.type
        if kind == "display_record":
            await self.conversation.display_record(data["record"], session_id=event.session_id, generation=event.generation)
        elif kind == "assistant_text_delta":
            await self.conversation.text_delta(event)
        elif kind == "state_snapshot":
            self.snapshot = data
            self.snapshot["session_id"] = event.session_id
            self.busy = data["busy"]
            self._controls()
        elif kind == "subagent_status":
            children = self.snapshot.setdefault("subagents", [])
            children[:] = [child for child in children if child["name"] != data["name"]]
            if data["status"] not in {"completed", "closed"}:
                children.append({"name": data["name"], "mode": data["mode"], "status": data["status"]})
                children.sort(key=lambda child: child["name"])
            self.query_one(SessionStatePanel).update_snapshot(self.snapshot, self.records)
        elif kind == "session_changed":
            self._dismiss_approval()
            self._activity = None
            self._diagnostics.clear()
            self._diagnostic_detail = None
            self._hidden_command_text = None
            self.query_one(CommandCandidates).show_commands(())
            await self.conversation.restore(data["records"], event.session_id, event.generation)
            self._controls()
        elif kind == "turn_started":
            self.turn_id = event.turn_id
            self.busy = True
            self.stopping = False
            self._controls()
            self._status("正在处理请求")
        elif kind == "turn_finished":
            self.turn_id = None
            self.busy = False
            self.stopping = False
            self._dismiss_approval()
            self._controls()
            outcome = data["outcome"]
            labels = {"completed": "完成", "failed": "失败", "stopped": "已停止", "truncated": "输出截断"}
            self._status("正在关闭" if self.closing else labels[outcome["status"]])
            if outcome["reason"]:
                self._log(outcome["reason"])
        elif kind == "command_result":
            if not data.get("turn_id"):
                self.busy = False
                self._controls()
                self._status("正在关闭" if self.closing else "就绪")
            result = data["data"]
            # 命令错误只弹窗提示，不再占用状态行；错误正文仍由展示记录留在对话区。
            if data["status"] == "failed":
                self.notify(result.get("error") or "命令执行失败", severity="error", markup=False)
            elif "sessions" in result and not self.closing:
                self.push_screen(SessionScreen(result["sessions"], self.snapshot.get("session_id")),
                                 lambda command: self._submit(command) if command else None)
            elif not data.get("recorded") and result.get("text"):
                await self.query_one("#chat", VerticalScroll).mount(Static(result["text"], markup=False))
        elif kind == "runtime_closed":
            self.runtime_closed = True
            self.exit(0 if data["saved"] else 1)
        elif kind == "log":
            self._log(data["text"])
        elif kind == "session_saved":
            if not self.busy:
                self._status("已保存")
        elif kind in {"runtime_error", "session_save_error"}:
            await self.query_one("#chat", VerticalScroll).mount(Static(data["error"], markup=False))
            self.notify(data["error"], severity="error")
        elif kind not in {"session_saved", "lead_usage", "user_message", "assistant_message", "tool_call",
                          "tool_result", "todo_updated", "background_status", "approval_requested", "approval_decided", "execution_status"}:
            self._log(f"[{event.source}] {kind}\n{json.dumps(data, ensure_ascii=False, indent=2)}")

    def on_unmount(self):
        if self._consume_timer is not None:
            self._consume_timer.stop()
        if self._clock_timer is not None:
            self._clock_timer.stop()
        # Emergency terminal detachment also releases approval waiters.
        self.runtime.close()
