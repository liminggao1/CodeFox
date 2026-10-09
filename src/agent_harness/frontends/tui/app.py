"""Textual adapter: UI state is a display copy, never business state."""

from __future__ import annotations

import json
import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, RichLog, Static, TextArea

from ...runtime.interactive_runtime import InteractiveRuntime
from ...runtime.local_commands import LOCAL_COMMANDS, find_command
from .widgets import ApprovalCard, CommandCandidates, ConversationRecord, SessionScreen


class HarnessApp(App):
    TITLE = "Agent Harness"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("up", "command_up", show=False, priority=True),
        Binding("down", "command_down", show=False, priority=True),
        Binding("tab", "command_fill", show=False, priority=True),
        Binding("enter", "command_enter", show=False, priority=True),
        Binding("escape", "command_hide", show=False, priority=True),
        Binding("ctrl+enter", "send", "发送", priority=True),
        Binding("ctrl+l", "clear_input", "清空", priority=True),
        Binding("ctrl+r", "sessions", "会话", priority=True),
        Binding("ctrl+c", "stop_turn", "停止", priority=True),
        Binding("ctrl+d", "close_runtime", "退出", priority=True),
        Binding("f2", "toggle_events", "事件", priority=True),
    ]
    CSS = """
    Screen { background: #151719; color: #e1e3e5; }
    #status { height: 2; padding: 0 1; background: #23272b; color: #80cbc4; }
    #body { height: 1fr; }
    #conversation { width: 2fr; height: 1fr; padding: 0 1; }
    #chat { height: 1fr; }
    #earlier { height: 3; display: none; }
    Markdown { padding: 0 1; margin: 0; background: transparent; }
    #side { width: 1fr; min-width: 28; max-width: 48; border-left: solid #44484c; }
    #state { height: auto; max-height: 45%; padding: 1; }
    #events-title { height: 1; padding-left: 1; color: #efb45b; }
    #events { height: 1fr; padding: 0 1; background: #1b1e21; }
    #input { height: 5; margin-top: 1; border: solid #55595d; }
    #command-hint { display: none; height: 1; color: #80cbc4; }
    #actions { height: 3; align-horizontal: right; }
    #actions Button { min-width: 8; margin-left: 1; }
    .compact #side { display: none; }
    .short #input { height: 3; margin-top: 0; }
    .short #actions { display: none; }
    Footer { background: #23272b; }
    """

    def __init__(self, runtime=None):
        super().__init__()
        self.runtime = runtime or InteractiveRuntime()
        self.generation = 0
        self.sequence = 0
        self.turn_id = None
        self.busy = False
        self.closing = False
        self.runtime_closed = False
        self.approval = None
        self.snapshot = {}
        self._hidden_command_text = None
        self.records = []
        self._record_ids = set()
        self._record_start = 0
        self._tool_names = {}
        self._consume_timer = None

    def compose(self) -> ComposeResult:
        yield Static("Agent Harness · 启动中", id="status", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="conversation"):
                yield Button("加载更早记录", id="earlier")
                yield VerticalScroll(id="chat")
                yield CommandCandidates(id="commands")
                yield Static(id="command-hint", markup=False)
                yield TextArea(id="input", soft_wrap=True, placeholder="消息或 /goal、/resume 命令")
                with Horizontal(id="actions"):
                    yield Button("停止", id="stop", disabled=True)
                    yield Button("发送", id="send", variant="primary")
            with Vertical(id="side"):
                yield Static(id="state", markup=False)
                yield Static("运行事件", id="events-title")
                yield RichLog(id="events", max_lines=1000, wrap=True, min_width=1, markup=False)
        yield Footer()

    def on_mount(self):
        self.runtime.start()
        self._consume_timer = self.set_interval(0.05, self.consume)
        self.query_one("#input", TextArea).focus()

    def on_resize(self, event):
        self.set_class(event.size.width < 85, "compact")
        self.set_class(event.size.height < 16, "short")

    def _controls(self):
        self.query_one("#send", Button).disabled = self.busy or self.closing
        self.query_one("#stop", Button).disabled = self.turn_id is None or self.closing
        self._refresh_commands()

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
        session = self.snapshot.get("session_id") or "新会话"
        model = self.snapshot.get("model", "")
        self.query_one("#status", Static).update(f"Agent Harness · {session} · {model}\n{label}")

    def _render_state(self):
        if not self.snapshot:
            return
        data = self.snapshot
        usage = data["lead_main_model_usage"]
        goal = data["goal"].get("active")
        lines = ["Goal", str(goal.get("condition")) if goal else "无活动目标", "", "TODO"]
        lines += [f"{item.get('status', '')} · {item.get('content', '')}" for item in data["todos"]] or ["无待办"]
        lines += ["", "子 Agent"] + [f"{child['name']} · {child['status']}" for child in data["subagents"]]
        lines += ["", "Lead 主模型（当前激活）", f"输入 {usage['input_tokens']} · 输出 {usage['output_tokens']}", f"请求 {usage['model_calls']}"]
        self.query_one("#state", Static).update("\n".join(lines))

    def action_send(self):
        if len(self.screen_stack) != 1:
            return
        self._send_draft()

    def _submit(self, text):
        if not text or not text.strip():
            return False
        result = self.runtime.submit(text)
        if result.status != "accepted":
            self.notify(result.reason, severity="warning")
            return False
        self.busy = True
        self._controls()
        self._status("执行中")
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

    def action_toggle_events(self):
        side = self.query_one("#side")
        side.display = not side.display

    def action_stop_turn(self):
        if self.turn_id and self.runtime.request_stop(self.turn_id):
            self._status("正在停止")
            self._dismiss_approval()

    def action_close_runtime(self):
        self.closing = True
        self.runtime.close()
        self._dismiss_approval()
        self._controls()
        self._status("正在关闭")

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "send":
            self.action_send()
        elif event.button.id == "stop":
            self.action_stop_turn()
        elif event.button.id == "earlier":
            await self._load_earlier()

    def _dismiss_approval(self):
        if self.approval is not None:
            for card in self.query(ApprovalCard):
                if card.request_id == self.approval.request_id:
                    card.disable()
            self.approval = None

    def on_approval_card_choice(self, event: ApprovalCard.Choice):
        request = self.approval
        if request is None or event.card.request_id != request.request_id:
            return
        if self.runtime.channel.respond(request.request_id, request.generation, event.decision):
            event.card.finish(event.decision)
            self.approval = None
            self.query_one("#input", TextArea).focus()

    def _record_widget(self, record):
        data = record["payload"]
        key = (record.get("turn_id"), record["source"], data.get("id") or data.get("tool_use_id"))
        if record["type"] == "tool_call":
            self._tool_names[key] = data["name"]
        return ConversationRecord(record, self._tool_names.get(key))

    async def _display_record(self, record):
        if record["id"] in self._record_ids:
            return
        self._record_ids.add(record["id"])
        self.records.append(record)
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(self._record_widget(record))
        if record["type"] == "approval_decided":
            for card in self.query(ApprovalCard):
                if card.request_id == record["payload"]["request_id"]:
                    card.finish(record["payload"]["decision"])
        while len(chat.children) > 80 and self.approval is None:
            first = chat.children[0]
            decided = {item["payload"]["request_id"] for item in self.records if item["type"] == "approval_decided"}
            if any(not card.historical and card.request_id not in decided for card in first.query(ApprovalCard)):
                break
            await chat.children[0].remove()
            if isinstance(first, ConversationRecord):
                self._record_start += 1
        self.query_one("#earlier", Button).display = self._record_start > 0
        chat.scroll_end(animate=False)

    async def _restore_records(self, records):
        self._dismiss_approval()
        self.records = list(records)
        self._record_ids = {record["id"] for record in records}
        self._record_start = max(0, len(records) - 80)
        self._tool_names = {}
        for record in records:
            if record["type"] == "tool_call":
                data = record["payload"]
                self._tool_names[(record.get("turn_id"), record["source"], data["id"])] = data["name"]
        await self._render_window()

    async def _render_window(self):
        chat = self.query_one("#chat", VerticalScroll)
        await chat.remove_children()
        if self.records[self._record_start:]:
            await chat.mount(*(self._record_widget(record) for record in self.records[self._record_start:]))
        decisions = {record["payload"]["request_id"]: record["payload"]["decision"] for record in self.records
                     if record["type"] == "approval_decided"}
        for card in self.query(ApprovalCard):
            if card.request_id in decisions:
                card.finish(decisions[card.request_id])
            elif self.approval is not None and card.request_id == self.approval.request_id:
                card.enable()
        self.query_one("#earlier", Button).display = self._record_start > 0
        chat.scroll_end(animate=False)

    async def _load_earlier(self):
        self._record_start = max(0, self._record_start - 80)
        await self._render_window()
        self.query_one("#chat", VerticalScroll).scroll_home(animate=False)

    def _log(self, text):
        self.query_one("#events", RichLog).write(Text.from_ansi(str(text)))

    async def consume(self):
        if not self.is_running:
            return
        # Drain a bounded batch so large output cannot starve UI input.
        for _ in range(100):
            event = self.runtime.channel.next_event()
            if event is None:
                break
            if event.scope == "session":
                self.generation = event.generation
            self.sequence = event.sequence
            try:
                await self._event(event)
            except Exception:
                self.runtime.close()
                raise
            self.runtime.channel.acknowledge_event(event.sequence)
            if not self.is_running:
                return
        if self.approval is None and not self.closing:
            request = self.runtime.channel.next_approval()
            if request is not None:
                self.approval = request
                card = next((card for card in self.query(ApprovalCard) if card.request_id == request.request_id), None)
                if card is None:
                    index = next((index for index, record in enumerate(self.records)
                                  if record["type"] == "approval_requested" and
                                  record["payload"]["request_id"] == request.request_id), None)
                    if index is not None:
                        self._record_start = min(self._record_start, index)
                        await self._render_window()
                        card = next(card for card in self.query(ApprovalCard) if card.request_id == request.request_id)
                if card is not None:
                    card.enable()
                    self.query_one("#chat", VerticalScroll).scroll_to_widget(card, animate=False)
        self._refresh_commands()

    async def _event(self, event):
        data = event.payload
        kind = event.type
        if kind == "display_record":
            await self._display_record(data["record"])
        elif kind == "state_snapshot":
            self.snapshot = data
            self.snapshot["session_id"] = event.session_id
            self.busy = data["busy"]
            self._render_state()
            self._controls()
            self._status("正在关闭" if self.closing else "执行中" if self.busy else "就绪")
        elif kind == "subagent_status":
            children = self.snapshot.setdefault("subagents", [])
            children[:] = [child for child in children if child["name"] != data["name"]]
            if data["status"] not in {"completed", "closed"}:
                children.append({"name": data["name"], "mode": data["mode"], "status": data["status"]})
                children.sort(key=lambda child: child["name"])
            self._render_state()
            self._log(f"[{event.source}] {kind}\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        elif kind == "session_changed":
            self._hidden_command_text = None
            self.query_one(CommandCandidates).show_commands(())
            self.query_one("#events", RichLog).clear()
            await self._restore_records(data["records"])
        elif kind == "turn_started":
            self.turn_id = event.turn_id
            self.busy = True
            self._controls()
            self._status("执行中")
        elif kind == "turn_finished":
            self.turn_id = None
            self.busy = False
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
                self._status("正在关闭" if self.closing else "失败" if data["status"] == "failed" else "就绪")
            result = data["data"]
            if "sessions" in result and not self.closing:
                self.push_screen(SessionScreen(result["sessions"], result["errors"]), lambda command: self._submit(command) if command else None)
            elif not data.get("recorded") and (result.get("text") or result.get("error")):
                await self.query_one("#chat", VerticalScroll).mount(Static(result.get("text") or result["error"], markup=False))
        elif kind == "runtime_closed":
            self.runtime_closed = True
            self.exit(0 if data["saved"] else 1)
        elif kind == "log":
            self._log(data["text"])
        elif kind in {"runtime_error", "session_save_error"}:
            self._log(data["error"])
            self.notify(data["error"], severity="error")
        elif kind not in {"session_saved", "lead_usage", "user_message", "assistant_message", "tool_call",
                          "tool_result", "todo_updated", "background_status", "approval_requested", "approval_decided"}:
            self._log(f"[{event.source}] {kind}\n{json.dumps(data, ensure_ascii=False, indent=2)}")

    def on_unmount(self):
        if self._consume_timer is not None:
            self._consume_timer.stop()
        # Emergency terminal detachment also releases approval waiters.
        self.runtime.close()
