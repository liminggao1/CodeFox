"""Textual adapter: UI state is a display copy, never business state."""

from __future__ import annotations

import json

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Markdown, RichLog, Static, TextArea

from ..interactive_runtime import InteractiveRuntime
from .widgets import ApprovalScreen, SessionScreen


class HarnessApp(App):
    TITLE = "Agent Harness"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
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
    .speaker { color: #80cbc4; margin-top: 1; height: 1; }
    .user-text { padding: 0 1; height: auto; color: #eeeeee; }
    Markdown { padding: 0 1; margin: 0; background: transparent; }
    #side { width: 1fr; min-width: 28; max-width: 48; border-left: solid #44484c; }
    #state { height: auto; max-height: 45%; padding: 1; }
    #events-title { height: 1; padding-left: 1; color: #efb45b; }
    #events { height: 1fr; padding: 0 1; background: #1b1e21; }
    #input { height: 5; margin-top: 1; border: solid #55595d; }
    #actions { height: 3; align-horizontal: right; }
    #actions Button { min-width: 8; margin-left: 1; }
    .compact #side { display: none; }
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

    def compose(self) -> ComposeResult:
        yield Static("Agent Harness · 启动中", id="status", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="conversation"):
                yield VerticalScroll(id="chat")
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
        self.set_interval(0.05, self.consume)
        self.query_one("#input", TextArea).focus()

    def on_resize(self, event):
        self.set_class(event.size.width < 85, "compact")

    def _controls(self):
        self.query_one("#send", Button).disabled = self.busy or self.closing
        self.query_one("#stop", Button).disabled = self.turn_id is None or self.closing

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
        editor = self.query_one("#input", TextArea)
        if self._submit(editor.text):
            editor.clear()

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
        self.query_one("#input", TextArea).clear()

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

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "send":
            self.action_send()
        elif event.button.id == "stop":
            self.action_stop_turn()

    def _dismiss_approval(self):
        if self.approval is not None:
            self.approval = None
            if isinstance(self.screen, ApprovalScreen):
                self.screen.dismiss("deny")

    async def _message(self, speaker, text, markdown=False):
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(Static(speaker, classes="speaker", markup=False),
                         Markdown(text) if markdown else Static(text, classes="user-text", markup=False))
        while len(chat.children) > 400:
            await chat.children[0].remove()
            await chat.children[0].remove()
        chat.scroll_end(animate=False)

    def _log(self, text):
        self.query_one("#events", RichLog).write(Text.from_ansi(str(text)))

    async def consume(self):
        # Drain a bounded batch so large output cannot starve UI input.
        for _ in range(100):
            event = self.runtime.channel.next_event()
            if event is None:
                break
            if event.sequence <= self.sequence:
                continue
            if event.scope == "session":
                if event.generation < self.generation:
                    continue
                self.generation = event.generation
            self.sequence = event.sequence
            await self._event(event)
        else:
            # Earlier turn notifications may still precede an approval in this batch.
            return
        if self.approval is None and not self.closing:
            request = self.runtime.channel.next_approval()
            if request is not None:
                if request.generation != self.generation or request.turn_id != self.turn_id:
                    self.runtime.channel.respond(request.request_id, request.generation, "deny")
                else:
                    self.approval = request
                    def decide(decision):
                        self.runtime.channel.respond(request.request_id, request.generation, decision)
                        self.approval = None
                    self.push_screen(ApprovalScreen(request), decide)

    async def _event(self, event):
        data = event.payload
        kind = event.type
        if kind == "state_snapshot":
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
        elif kind == "user_message":
            await self._message("你", data["text"])
        elif kind == "assistant_message":
            await self._message("Lead", data["text"], markdown=True)
        elif kind == "session_changed":
            await self.query_one("#chat", VerticalScroll).remove_children()
            self.query_one("#events", RichLog).clear()
            for message in data["history"]:
                speaker = "你" if message["role"] == "user" else "Lead"
                content = message["content"]
                if isinstance(content, str):
                    await self._message(speaker, content, markdown=message["role"] == "assistant")
                else:
                    for block in content:
                        if block.get("type") == "text":
                            await self._message(speaker, block["text"], markdown=message["role"] == "assistant")
                        elif block.get("type") in {"tool_use", "tool_result"}:
                            self._log(json.dumps(block, ensure_ascii=False))
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
            elif result.get("text") or result.get("error"):
                self._log(result.get("text") or result["error"])
        elif kind == "runtime_closed":
            self.runtime_closed = True
            self.exit(0 if data["saved"] else 1)
        elif kind == "log":
            self._log(data["text"])
        elif kind in {"runtime_error", "session_save_error"}:
            self._log(data["error"])
            self.notify(data["error"], severity="error")
        elif kind not in {"session_saved", "lead_usage"}:
            self._log(f"[{event.source}] {kind}\n{json.dumps(data, ensure_ascii=False, indent=2)}")

    def on_unmount(self):
        # Emergency terminal detachment also releases approval waiters.
        self.runtime.close()
