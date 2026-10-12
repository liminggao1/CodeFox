"""Textual adapter: UI state is a display copy, never business state."""

from __future__ import annotations

import json
import re

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, TextArea

from ...runtime.interactive_runtime import InteractiveRuntime
from ...runtime.local_commands import LOCAL_COMMANDS, find_command
from ...runtime.permissions import MODE_LABELS
from .widgets import (ApprovalCard, BrandBar, CommandCandidates, PermissionModeScreen, SessionScreen, SessionStatePanel, SettingsScreen)
from ..presentation import (model_status,
                            permission_summary, session_title)
from .conversation_view import ConversationView
from .themes import DEFAULT_THEME, THEMES

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
    Screen { background: $background; color: $foreground; }
    #workspace { height: 1fr; }
    #conversation { width: 1fr; height: 1fr; padding: 0 1; }
    #conversation-top { height: 1; }
    #conversation-top #earlier { width: auto; min-width: 0; height: 1; min-height: 1; border: none; padding: 0 1; }
    #chat { height: 1fr; }
    #earlier { display: none; }
    Markdown { padding: 0 1; margin: 0; background: transparent; color: $foreground; }
    .activity { height: auto; color: $codefox-muted; margin: 1 0; }
    .diagnostic { height: auto; padding: 0; color: $codefox-muted; background: $surface; }
    .diagnostic * { color: $codefox-muted; }
    #command-hint { display: none; height: 1; color: $primary; }
    #input-area { height: auto; padding: 0 1; }
    #input-status { height: 1; color: $codefox-muted; }
    #input-status.processing { color: $primary; }
    #input-status.completed { color: $success; }
    #input-status.failed { color: $error; }
    #input-status.stopped { color: $warning; }
    #input-box { height: 4; margin-top: 1; border: solid $codefox-border; background: $surface; }
    #input-row { height: 1; }
    #input { width: 1fr; height: 1; border: none; background: transparent; padding: 0 1; }
    #send { width: auto; min-width: 8; height: 1; min-height: 1; border: none; margin: 0 1; padding: 0 1; }
    #input-hint { height: 1; color: $codefox-muted; padding: 0 1; }
    #input-footer { height: 1; color: $codefox-muted; }
    #permission { width: auto; min-width: 0; height: 1; min-height: 1;
        border: none; padding: 0 1; background: transparent; color: $foreground; }
    #permission.no-approval { color: $warning; text-style: bold; }
    #footer-spacer { width: 1fr; }
    #bottom-status { width: auto; color: $codefox-muted; }
    #bottom-status.context-warning { color: $warning; }
    #bottom-status.context-full { color: $error; }
    .short #input-box { height: 3; margin-top: 0; }
    .short #input-hint { display: none; }
    .short #input-footer { display: none; }
    .short #input-status { display: none; }
    .short BrandBar { height: 1; }
    .short BrandBar .brand, .short BrandBar .session-title { height: 1; padding: 0; }
    """

    def __init__(self, runtime=None):
        super().__init__()
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME
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
        self.short = False
        self._hidden_command_text = None
        self.conversation = ConversationView(self)
        self._consume_timer = None
        self._clock_timer = None

    @property
    def records(self):
        return self.conversation.records

    def compose(self) -> ComposeResult:
        yield BrandBar()
        with Horizontal(id="workspace"):
            with Vertical(id="conversation"):
                with Horizontal(id="conversation-top"):
                    yield Button("加载更早记录", id="earlier")
                yield VerticalScroll(id="chat")
                yield CommandCandidates(id="commands")
                yield Static(id="command-hint", markup=False)
            yield SessionStatePanel(id="session-status")
        # 输入区在主体之外：展开状态面板时对话列让位，输入框不被压缩。
        with Vertical(id="input-area"):
            yield Static("", id="input-status", markup=False)
            with Vertical(id="input-box"):
                with Horizontal(id="input-row"):
                    yield TextArea(id="input", soft_wrap=True, placeholder="输入消息或 / 命令…")
                    yield Button("发送", id="send", variant="primary")
                yield Static("Enter 发送 · Shift+Enter 换行", id="input-hint", markup=False)
            with Horizontal(id="input-footer"):
                yield Button("权限：默认 ▾", id="permission", compact=True)
                yield Static(id="footer-spacer")
                yield Static(id="bottom-status", markup=False)

    def on_mount(self):
        self.runtime.start()
        self._consume_timer = self.set_interval(0.05, self.consume)
        self._clock_timer = self.set_interval(0.25, self.conversation.refresh_elapsed)
        self.query_one("#input", TextArea).focus()
        self._controls()

    def on_resize(self, event):
        self.short = event.size.height < 16
        self.set_class(self.short, "short")
        self._controls()

    def on_key(self, event):
        """Keep Home/End deterministic when the conversation itself has focus."""
        if event.key == "shift+enter" and self.focused is self.query_one("#input", TextArea):
            self.query_one("#input", TextArea).insert("\n")
            event.stop()
            return
        if event.key not in {"home", "end"}:
            return
        if self.conversation.scroll_key(event.key):
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
            button.disabled = self._context_full() and not self._draft_is_compact()
        footer = self.query_one("#bottom-status", Static)
        footer.update(model_status(self.snapshot))
        footer.remove_class("context-warning", "context-full")
        used = self.snapshot.get("context_input_tokens")
        limit = self.snapshot.get("context_limit_tokens")
        if isinstance(used, int) and isinstance(limit, int) and limit > 0:
            if used >= limit:
                footer.add_class("context-full")
            elif used * 100 >= limit * 80:
                footer.add_class("context-warning")
        self._input_status()
        self._set_permission()
        self.query_one(SessionStatePanel).update_snapshot(self.snapshot, self.records)
        self.query_one(SessionStatePanel).display = self.state_expanded
        self.query_one(BrandBar).update_title(session_title(self.snapshot, self.records))
        self._refresh_commands()

    def _set_permission(self):
        mode = self.runtime.permission_mode
        button = self.query_one("#permission", Button)
        button.label = permission_summary(MODE_LABELS[mode]) + " ▾"
        button.set_class(mode == "no_approval", "no-approval")
        button.disabled = self.busy or self.approval is not None or self.closing or self.runtime_closed

    def _permission_selected(self, mode):
        if mode is not None:
            try:
                self.runtime.set_permission_mode(mode)
            except (OSError, ValueError) as exc:
                self.notify(f"权限设置保存失败：{exc}", severity="error")
            self._controls()
        self._focus_input()

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
        # 满载时仅允许 /compact；草稿变化后立即同步发送按钮状态。
        self._controls()

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

    def _context_full(self):
        used = self.snapshot.get("context_input_tokens")
        limit = self.snapshot.get("context_limit_tokens")
        return (isinstance(used, int) and isinstance(limit, int)
                and limit > 0 and used >= limit)

    def _draft_is_compact(self):
        text = self.query_one("#input", TextArea).text.strip()
        return bool(text) and text.split(maxsplit=1)[0] == "/compact"

    def _status(self, label=""):
        """底部状态行始终更新；对话区的活动行只在执行期间存在。"""
        self._notice = label if label in {"处理中", "完成", "失败", "已停止"} else None
        self._input_status()
        self.conversation.show_activity(label)

    def _input_status(self):
        """Input-area status line: runtime state first, then the last notice."""
        if self.busy:
            label = "处理中"
        else:
            label = self._notice or ""
        status = self.query_one("#input-status", Static)
        status.update(label)
        for name, text in (("processing", "处理中"), ("completed", "完成"),
                           ("failed", "失败"), ("stopped", "已停止")):
            status.set_class(label == text, name)

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
        if self._context_full() and text.strip().split(maxsplit=1)[0] != "/compact":
            self.notify("上下文已满，请先执行 /compact", severity="warning", markup=False)
            return False
        result = self.runtime.submit(text)
        if result.status != "accepted":
            self.notify(result.reason, severity="warning")
            return False
        self.busy = True
        if result.kind == "turn":
            self.conversation.show_pending_input(result.submission_id, text)
        self._controls()
        self._status("处理中")
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
        elif event.button.id == "state":
            self.state_expanded = not self.state_expanded
            self._controls()
        elif event.button.id == "new-session":
            self.action_new_session()
        elif event.button.id == "settings":
            self.push_screen(SettingsScreen(self.conversation.diagnostics_visible),
                             lambda _: self._focus_input())
        elif event.button.id == "permission" and not self.busy and self.approval is None:
            self.push_screen(PermissionModeScreen(self.runtime.permission_mode), self._permission_selected)
        self._focus_input()

    def on_settings_screen_diagnostics_changed(self, event: SettingsScreen.DiagnosticsChanged):
        self.conversation.set_diagnostics_visible(event.visible)

    def on_settings_screen_theme_changed(self, event: SettingsScreen.ThemeChanged):
        self.theme = event.theme

    def _focus_input(self):
        """顶部入口和状态切换不保留焦点，输入框始终可以继续编辑草稿。"""
        if self.is_running:
            self.query_one("#input", TextArea).focus()

    def _dismiss_approval(self):
        if self.approval is not None:
            self.conversation.disable_approval(self.approval.request_id)
            self.approval = None
        if self.is_running:
            self._set_permission()

    def on_approval_card_choice(self, event: ApprovalCard.Choice):
        request = self.approval
        if request is None or event.card.request_id != request.request_id:
            return
        if self.runtime.channel.respond(request.request_id, request.generation, event.decision):
            self.conversation.finish_approval(request.request_id, event.decision)
            self.approval = None
            self._set_permission()
            self.query_one("#input", TextArea).focus()

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
                self._set_permission()
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
            self._hidden_command_text = None
            self.query_one(CommandCandidates).show_commands(())
            await self.conversation.restore(data["records"], event.session_id, event.generation)
            self._controls()
        elif kind == "turn_started":
            self.turn_id = event.turn_id
            self.busy = True
            self.stopping = False
            self._controls()
            self._status("处理中")
        elif kind == "turn_finished":
            self.turn_id = None
            self.busy = False
            self.stopping = False
            self._dismiss_approval()
            self._controls()
            outcome = data["outcome"]
            labels = {"completed": "完成", "failed": "失败", "stopped": "已停止", "truncated": "失败"}
            self._status(labels[outcome["status"]])
            summary = data.get("summary") or {}
            usage = summary.get("lead_usage") or {}
            self.conversation.log(
                f"[TURN] {labels[outcome['status']]} · {summary.get('elapsed_seconds', 0):.1f}s"
                f" · 工具 {summary.get('tool_calls', 0)} · 审批 {summary.get('approvals', 0)}"
                f" · 请求 {usage.get('model_calls', 0)}"
                f" · 输入 {usage.get('input_tokens', 0)} · 输出 {usage.get('output_tokens', 0)}")
            if outcome["reason"]:
                self.conversation.log(outcome["reason"])
        elif kind == "command_result":
            if not data.get("turn_id"):
                self.busy = False
                self._notice = None
                self._controls()
                self._input_status()
                self.conversation.show_activity("")
            result = data["data"]
            # 命令结果一律弹窗提示：对话区不留命令行，避免开发者视角的噪音。
            if data["status"] == "failed":
                self.notify(result.get("error") or "命令执行失败", severity="error", markup=False)
            elif "sessions" in result and not self.closing:
                self.push_screen(SessionScreen(result["sessions"], self.snapshot.get("session_id")),
                                 lambda command: self._submit(command) if command else None)
            elif result.get("text") and not self.closing:
                self.notify(str(result["text"]), markup=False)
        elif kind == "runtime_closed":
            self.runtime_closed = True
            self.exit(0 if data["saved"] else 1)
        elif kind == "log":
            self.conversation.log(data["text"])
        elif kind == "session_saved":
            pass
        elif kind == "runtime_error":
            self.conversation.log(data["error"])
            self.notify(data["error"], severity="error")
        elif kind == "session_save_error":
            await self.conversation.show_error(data["error"])
            self.notify(data["error"], severity="error")
        elif kind not in {"session_saved", "lead_usage", "user_message", "assistant_message", "tool_call",
                          "tool_result", "todo_updated", "background_status", "approval_requested", "approval_decided",
                          "execution_status", "system_error"}:
            self.conversation.log(f"[{event.source}] {kind}\n{json.dumps(data, ensure_ascii=False, indent=2)}")

    def on_unmount(self):
        if self._consume_timer is not None:
            self._consume_timer.stop()
        if self._clock_timer is not None:
            self._clock_timer.stop()
        # Emergency terminal detachment also releases approval waiters.
        self.runtime.close()
