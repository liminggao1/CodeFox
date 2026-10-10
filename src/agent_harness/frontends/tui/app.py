"""Textual adapter: UI state is a display copy, never business state."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Static, TextArea

from ...runtime.interactive_runtime import InteractiveRuntime
from ...runtime.local_commands import LOCAL_COMMANDS, find_command
from .widgets import (ApprovalCard, BrandBar, CommandCandidates, ConversationRecord, DetailScroll,
                      OperationEntry, ProcessGroup, SessionInfoScreen, SessionScreen, SessionStatePanel,
                      StreamingText)
from ..presentation import operation_key, session_title, session_status_label


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
    #chat { height: 1fr; }
    #earlier { height: 3; display: none; }
    Markdown { padding: 0 1; margin: 0; background: transparent; }
    .activity { height: auto; color: #999fa5; margin: 1 0; }
    .diagnostic { height: auto; padding: 0; }
    #input { height: 5; margin-top: 1; border: solid #55595d; background: #202326; }
    #command-hint { display: none; height: 1; color: #80cbc4; }
    #actions { height: 3; align-horizontal: right; }
    #actions Button { min-width: 8; margin-left: 1; }
    #input-footer { height: 1; color: #999fa5; }
    #permission { width: 1fr; color: #b3a35a; }
    #bottom-status { width: auto; color: #999fa5; }
    .short #input { height: 3; margin-top: 0; }
    .short #actions { display: none; }
    .short #input-footer { display: none; }
    .short BrandBar { height: 1; }
    .short BrandBar .brand, .short BrandBar .session-title { height: 1; padding: 0; }
    .short BrandBar #top-actions { width: 6; }
    .short BrandBar #top-actions Button { display: none; }
    .short BrandBar #top-actions #state { display: block; min-width: 5; margin-left: 0; }
    .narrow #session-status { display: none; }
    .narrow #conversation { width: 1fr; }
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
        self.runtime_closed = False
        self.approval = None
        self.snapshot = {}
        self._hidden_command_text = None
        self.records = []
        self._record_ids = set()
        self._record_start = 0
        self._tool_names = {}
        self._consume_timer = None
        self._clock_timer = None
        self._activity = None
        self._pending_inputs = {}
        self._diagnostics = []
        self._diagnostic_detail = None
        self._operations = {}
        self._groups = {}
        self._streams = {}

    def compose(self) -> ComposeResult:
        yield BrandBar()
        with Horizontal(id="workspace"):
            with Vertical(id="conversation"):
                yield Button("加载更早记录", id="earlier")
                yield VerticalScroll(id="chat")
                yield CommandCandidates(id="commands")
                yield Static(id="command-hint", markup=False)
                yield TextArea(id="input", soft_wrap=True, placeholder="消息或 / 命令 · Enter 发送 · Shift+Enter 换行")
                with Horizontal(id="actions"):
                    yield Button("发送", id="send", variant="primary")
                with Horizontal(id="input-footer"):
                    yield Static("权限：按工具策略", id="permission", markup=False)
                    yield Static("模型：未开始 · 状态：就绪", id="bottom-status", markup=False)
            yield SessionStatePanel(id="session-status")

    def on_mount(self):
        self.runtime.start()
        self._consume_timer = self.set_interval(0.05, self.consume)
        self._clock_timer = self.set_interval(0.25, self._refresh_operation_elapsed)
        self.query_one("#input", TextArea).focus()

    def on_resize(self, event):
        self.set_class(event.size.height < 16, "short")
        self.set_class(event.size.width < 90, "narrow")

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
        if self.closing:
            button.label = "停止中"
            button.disabled = True
        elif self.stopping:
            button.label = "停止中"
            button.disabled = True
        elif self.busy:
            button.label = "停止"
            button.disabled = self.turn_id is None
        else:
            button.label = "发送"
            button.disabled = False
        status = session_status_label({"busy": self.busy, "closing": self.closing})
        self.query_one("#bottom-status", Static).update(
            f"模型：{self.snapshot.get('model') or '未开始'} · 状态：{status} · 消息：{len(self.records)}")
        self.query_one(SessionStatePanel).update_snapshot(self.snapshot, self.records)
        self.query_one(BrandBar).update_title(session_title(self.snapshot, self.records))
        self._refresh_commands()

    def _set_permission(self, text):
        self.query_one("#permission", Static).update("权限：" + text)

    def _refresh_operation_elapsed(self):
        if not self.is_running:
            return
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for entry in self.query(OperationEntry):
            entry.refresh_elapsed(now)

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
        if self._activity is None or self._activity.parent is None:
            if label == "就绪":
                return
            self._activity = Static(label, classes="activity", markup=False)
            self.query_one("#chat", VerticalScroll).mount(self._activity)
        else:
            self._activity.update(label)

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
            self._pending_inputs[result.submission_id] = pending
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

    def action_stop_turn(self):
        if self.turn_id and self.runtime.request_stop(self.turn_id):
            self.stopping = True
            self._controls()
            self._status("正在停止")
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
            await self._load_earlier()
        elif event.button.id == "session" or event.button.id == "more":
            self.action_sessions()
        elif event.button.id == "state":
            self.push_screen(SessionInfoScreen(self.snapshot, self.records))
        elif event.button.id == "history":
            self._record_start = 0
            await self._render_window()
            self.query_one("#chat", VerticalScroll).scroll_home(animate=False)
        elif event.button.id == "settings":
            self.push_screen(SessionInfoScreen(self.snapshot, self.records))

    def _dismiss_approval(self):
        if self.approval is not None:
            for card in self.query(ApprovalCard):
                if card.request_id == self.approval.request_id:
                    card.disable()
            self.approval = None
        if self.is_running:
            self._set_permission("按工具策略")

    def on_approval_card_choice(self, event: ApprovalCard.Choice):
        request = self.approval
        if request is None or event.card.request_id != request.request_id:
            return
        if self.runtime.channel.respond(request.request_id, request.generation, event.decision):
            event.card.finish(event.decision)
            self.approval = None
            self._set_permission("按工具策略")
            self.query_one("#input", TextArea).focus()

    def _record_widget(self, record):
        data = record["payload"]
        key = (record.get("turn_id"), record["source"], data.get("id") or data.get("tool_use_id"))
        if record["type"] == "tool_call":
            self._tool_names[key] = data["name"]
        return ConversationRecord(record, self._tool_names.get(key))

    async def _mount_record(self, record):
        chat = self.query_one("#chat", VerticalScroll)
        data, kind = record["payload"], record["type"]
        if kind == "assistant_message" and data.get("model_call_id"):
            stream = self._streams.get((record.get("turn_id"), record["source"], data["model_call_id"], data.get("block_index", 0)))
            if stream is not None and stream.is_mounted:
                stream.confirm(data["text"])
                return
        if kind == "execution_status" or (kind in {"tool_call", "tool_result"} and data.get("model_call_id")):
            key = operation_key(record)
            entry = self._operations.get(key)
            if entry is None or not entry.is_mounted:
                parent = chat
                if data.get("model_call_id") and data.get("phase") != "model":
                    group_key = (record.get("turn_id"), record["source"], data["model_call_id"])
                    group = self._groups.get(group_key)
                    if group is None or not group.is_mounted:
                        group = ProcessGroup(record["source"])
                        group.record_start_index = max(0, len(self.records) - 1)
                        self._groups[group_key] = group
                        await chat.mount(group)
                    parent = group.query_one(VerticalScroll)
                entry = OperationEntry(record)
                entry.record_start_index = max(0, len(self.records) - 1)
                self._operations[key] = entry
                await parent.mount(entry)
            entry.apply(record)
            if kind == "execution_status" and data["phase"] == "model" and data["state"] in {"failed", "stopped", "discarded"}:
                for stream_key, stream in self._streams.items():
                    if stream_key[2] == data.get("model_call_id") and not stream.confirmed:
                        stream.discard()
            return
        widget = self._record_widget(record)
        widget.record_start_index = next((i for i, item in enumerate(self.records) if item["id"] == record["id"]), 0)
        await chat.mount(widget)

    async def _text_delta(self, event):
        data = event.payload
        key = (event.turn_id, event.source, data["model_call_id"], data["block_index"])
        stream = self._streams.get(key)
        chat = self.query_one("#chat", VerticalScroll)
        follow = chat.scroll_y >= chat.max_scroll_y - 1 and self.approval is None
        if stream is None:
            stream = StreamingText()
            self._streams[key] = stream
            await chat.mount(stream)
        stream.append(data)
        if follow:
            chat.scroll_end(animate=False)

    async def _display_record(self, record):
        if record["id"] in self._record_ids:
            return
        self._record_ids.add(record["id"])
        self.records.append(record)
        if self.is_running:
            self.query_one(BrandBar).update_title(session_title(self.snapshot, self.records))
        chat = self.query_one("#chat", VerticalScroll)
        follow = chat.scroll_y >= chat.max_scroll_y - 1 and self.approval is None
        pending = self._pending_inputs.pop(record["payload"].get("submission_id"), None) if record["type"] == "user_message" else None
        if pending is not None:
            pending.record = record
        else:
            await self._mount_record(record)
        if record["type"] == "approval_decided":
            for card in self.query(ApprovalCard):
                if card.request_id == record["payload"]["request_id"]:
                    card.finish(record["payload"]["decision"])
        while len(chat.children) > 80 and self.approval is None and not self.busy:
            first = chat.children[0]
            decided = {item["payload"]["request_id"] for item in self.records if item["type"] == "approval_decided"}
            if any(not card.historical and card.request_id not in decided for card in first.query(ApprovalCard)):
                break
            await chat.children[0].remove()
            if hasattr(first, "record_start_index"):
                self._record_start = max(self._record_start, first.record_start_index + 1)
        self.query_one("#earlier", Button).display = self._record_start > 0
        if follow:
            chat.scroll_end(animate=False)

    async def _restore_records(self, records):
        self._dismiss_approval()
        self._activity = None
        self._pending_inputs.clear()
        self._diagnostics.clear()
        self._diagnostic_detail = None
        self._streams.clear()
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
        temporary = [(key, stream.text, stream.sequence, stream.discarded) for key, stream in self._streams.items()
                     if stream.is_mounted and not any(r["type"] == "assistant_message" and
                     (r.get("turn_id"), r["source"], r["payload"].get("model_call_id"), r["payload"].get("block_index", 0)) == key
                     for r in self.records)]
        await chat.remove_children()
        self._streams.clear()
        self._operations.clear()
        self._groups.clear()
        visible = self.records[self._record_start:]
        visible_ids = {r["id"] for r in visible}
        group_ids = {(r.get("turn_id"), r["source"], r["payload"].get("model_call_id")) for r in visible if r["payload"].get("model_call_id")}
        for record in self.records:
            group_key = (record.get("turn_id"), record["source"], record["payload"].get("model_call_id"))
            if record["id"] in visible_ids or group_key in group_ids:
                await self._mount_record(record)
        for key, text, sequence, discarded in temporary:
            stream = StreamingText()
            self._streams[key] = stream
            await chat.mount(stream)
            stream.append({"sequence": sequence, "delta": text})
            if discarded:
                stream.discard()
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
            if event.type == "assistant_text_delta":
                def key(item):
                    return (item.session_id, item.generation, item.turn_id, item.source,
                            item.payload.get("model_call_id"), item.payload.get("block_index"))
                payload = dict(event.payload)
                while index < len(batch) and batch[index].type == event.type and key(batch[index]) == key(event):
                    following = batch[index]
                    if following.payload["sequence"] > payload["sequence"]:
                        payload["delta"] += following.payload["delta"]
                        payload["sequence"] = following.payload["sequence"]
                    acknowledged.append(following)
                    index += 1
                event = replace(event, payload=payload)
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
                self._set_permission("等待审批")
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
                    chat = self.query_one("#chat", VerticalScroll)
                    chat.scroll_to_widget(card, animate=False)
                    chat.scroll_end(animate=False)
        self._refresh_commands()

    async def _event(self, event):
        data = event.payload
        kind = event.type
        if kind == "display_record":
            await self._display_record(data["record"])
        elif kind == "assistant_text_delta":
            await self._text_delta(event)
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
            self._hidden_command_text = None
            self.query_one(CommandCandidates).show_commands(())
            await self._restore_records(data["records"])
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
