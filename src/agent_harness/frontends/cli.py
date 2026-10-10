"""Command routing and the sole stdin reader for the interactive CLI."""

from __future__ import annotations

import asyncio
import json
import sys
import threading

from .. import core
from ..workflows import workflow_runtime, workflow_demo
from ..runtime.interactive_runtime import InteractiveRuntime
from .presentation import STATUS_LABELS, arguments_summary, lifecycle_summary, short_text


class CLIAdapter:
    def __init__(self, runtime, reader=None, write=None, write_chunk=None):
        self.runtime = runtime
        self.reader = reader or core.CONSOLE.ask
        self.write = write or core.terminal_print
        self.write_chunk = write_chunk or (write if write is not None else lambda text: print(text, end="", flush=True))
        self.closed = False
        self._completed = set()
        self._changed = threading.Event()
        self._renderer = None
        self._record_ids = set()
        self._tool_names = {}
        self._streamed = {}
        self._stream_open = None

    def _end_stream_line(self):
        if self._stream_open is not None:
            self.write_chunk("\n")
            self._stream_open = None

    def _discard_stream(self, data):
        """Close a failed attempt and make its discarded text explicit."""
        call_id = data.get("model_call_id")
        if not call_id:
            return
        for key, stream in self._streamed.items():
            if key[2] != call_id or stream.get("discarded"):
                continue
            self._end_stream_line()
            self.write("本次输出未采用")
            stream["discarded"] = True

    def render_record(self, record):
        if record["id"] in self._record_ids:
            return
        self._record_ids.add(record["id"])
        kind, data, source = record["type"], record["payload"], record["source"]
        if kind == "assistant_message" and data.get("model_call_id"):
            stream_key = (record.get("turn_id"), source, data["model_call_id"], data.get("block_index", 0))
            if stream_key in self._streamed:
                self._end_stream_line()
                return
        self._end_stream_line()
        key = (record.get("turn_id"), source, data.get("id") or data.get("tool_use_id"))
        if kind in {"user_message", "assistant_message"}:
            self.write(f"[{ '你' if kind == 'user_message' else source }] {data['text']}")
        elif kind == "tool_call":
            self._tool_names[key] = data["name"]
            self.write(f"[{source}] 工具 · {data['name']} · {arguments_summary(data.get('input', {}))} · 待执行")
            if data.get("result_missing"):
                self.write("结果未记录，请核对当前状态")
        elif kind == "tool_result":
            status = STATUS_LABELS.get(data.get("status"), "已返回结果")
            content = data.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            self.write(f"[{source}] {self._tool_names.get(key, '工具')} · {status}\n{short_text(content)}")
        elif kind == "execution_status":
            self.write(f"[{source}] {lifecycle_summary(data)}" + ("\n" + short_text(data["reason"]) if data.get("reason") else ""))
            if data.get("phase") == "model" and data.get("state") in {"failed", "stopped", "discarded"}:
                self._discard_stream(data)
        elif kind == "todo_updated":
            self.write(f"[{source}] TODO\n" + ("\n".join(f"{item.get('status', '')} · {item.get('content', '')}"
                                                      for item in data.get("todos", [])) or "无待办"))
        elif kind in {"subagent_status", "background_status"}:
            suffix = " · 历史记录" if data.get("historical") else ""
            self.write(f"[{source}] {data.get('name') or data.get('task_id', '后台任务')} · {data['status']}{suffix}")
        elif kind == "approval_requested":
            status = "上次中断，未继续执行" if data.get("interrupted") else "历史审批" if data.get("historical") else "等待审批"
            self.write(f"[{source}] {data['tool_name']} · {status}\n{json.dumps(data.get('arguments', {}), ensure_ascii=False, indent=2)}")
        elif kind == "approval_decided":
            status = {"approve": "已批准", "deny": "已拒绝", "closed": "已关闭"}.get(data["decision"], data["decision"])
            self.write(f"[{source}] {data.get('tool_name', '工具')} · {status}")
        elif kind == "command_result":
            result = data.get("data", {})
            if result.get("text") or result.get("error"):
                self.write(result.get("text") or result["error"])
        elif kind == "turn_finished":
            outcome, summary = data.get("outcome", {}), data.get("summary", {})
            status = outcome.get("status", data.get("status", "completed"))
            text = f"[{status}]"
            if summary:
                text += f" {summary.get('elapsed_seconds', 0):.1f}s · 工具 {summary.get('tool_calls', 0)} · 审批 {summary.get('approvals', 0)}"
                usage = summary.get("lead_usage", {})
                text += f"\nLead 主模型 · 请求 {usage.get('model_calls', 0)} · 输入 {usage.get('input_tokens', 0)} · 输出 {usage.get('output_tokens', 0)}"
            if outcome.get("reason"):
                text += "\n" + outcome["reason"]
            self.write(text)

    def render(self, event):
        payload = event.payload
        if event.type == "assistant_text_delta":
            key = (event.turn_id, event.source, payload["model_call_id"], payload["block_index"])
            previous = self._streamed.get(key, {"sequence": 0, "text": ""})
            if previous.get("discarded") or payload["sequence"] <= previous["sequence"]:
                return
            if self._stream_open != key:
                self._end_stream_line()
                self.write_chunk(f"[{event.source}] ")
                self._stream_open = key
            self.write_chunk(payload["delta"])
            self._streamed[key] = {"sequence": payload["sequence"], "text": previous["text"] + payload["delta"]}
        elif event.type == "display_record":
            self.render_record(payload["record"])
        elif event.type == "session_changed":
            self._record_ids.clear()
            self._tool_names.clear()
            self._end_stream_line()
            self._streamed.clear()
            for record in payload["records"]:
                self.render_record(record)
        elif event.type == "log":
            self._end_stream_line()
            self.write(payload["text"])
        elif event.type == "command_result":
            data = payload["data"]
            for entry in data.get("sessions", []):
                self.write(f"{entry['id']}  {entry['updated_at']}  {entry['message_count']} messages  {entry['summary']}")
            for error in data.get("errors", []):
                self.write(f"[session error] {error}")
            if "sessions" in data and not data["sessions"]:
                self.write("No saved sessions")
            if not payload.get("recorded") and (data.get("text") or data.get("error")):
                self.write(data.get("text") or data["error"])
        elif event.type in {"runtime_error", "session_save_error"}:
            self._end_stream_line()
            self.write(payload["error"])
        elif event.type == "runtime_closed":
            self._end_stream_line()
            self.closed = True
            if not payload["saved"]:
                self.write("Session could not be saved")

    def pump(self, submission_id=None):
        """Wait for accepted work while collecting approvals on this same thread."""
        while not self.closed:
            request = self.runtime.channel.next_approval()
            if request is not None:
                try:
                    answer = self.reader("Allow? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    self.runtime.channel.respond(request.request_id, request.generation, "deny")
                    self.runtime.close()
                else:
                    self.runtime.channel.respond(request.request_id, request.generation,
                                                 "approve" if answer in {"y", "yes"} else "deny")
            if submission_id in self._completed:
                self._completed.remove(submission_id)
                return
            self._changed.wait(0.05)
            self._changed.clear()

    def _render_events(self):
        # This adapter thread renders idle background output; it never reads stdin.
        display_failed = False
        while not self.closed:
            event = self.runtime.channel.next_event(timeout=0.05)
            if event is None:
                continue
            if display_failed:
                self.closed = event.type == "runtime_closed"
                self._changed.set()
                continue
            try:
                self.render(event)
            except Exception:
                # Continue draining until owned execution has shut down.
                display_failed = True
                self.runtime.close()
                self._changed.set()
                continue
            self.runtime.channel.acknowledge_event(event.sequence)
            if (event.type == "turn_finished" or
                    event.type == "command_result" and not event.payload.get("turn_id")):
                self._completed.add(event.payload.get("submission_id"))
            self._changed.set()

    def run(self):
        """运行命令行交互，直到用户退出或运行时关闭。

        当前线程读取输入和审批；运行时的工作线程执行请求；
        cli-output 线程读取事件并显示模型回复、工具状态等结果。
        """
        # 标记 CLI 已启动，让后台打印配合输入提示符显示。
        core.CLI_ACTIVE = True
        # 初始化运行时，并启动执行请求的工作线程；此时还没有用户请求。
        self.runtime.start()
        # 创建显示线程：target 是线程启动后执行的函数，不在这里立即调用。
        # name 是线程名；daemon=True 表示它本身不阻止进程退出，正常收尾仍会等待它结束。
        self._renderer = threading.Thread(target=self._render_events, name="cli-output", daemon=True)
        # 启动显示线程，持续读取事件；它不读取用户输入。
        self._renderer.start()
        try:
            # 每次循环读取一条输入；运行时关闭后 self.closed 会变为 True。
            while not self.closed:
                # 显示提示符并等待输入，用户按回车后才返回字符串。
                text = self.reader()
                # 去掉首尾空白并转为小写；空输入或退出命令会结束交互循环。
                if text.strip().lower() in {"", "q", "exit", "/quit"}:
                    break
                # 将输入交给运行时排队，返回是否接收及请求 ID；模型在工作线程执行。
                result = self.runtime.submit(text)
                if result.status == "accepted":
                    # 等待这次请求完成，期间在当前线程处理审批输入。
                    # 模型回复和工具状态由显示线程持续输出，完成后才读取下一条输入。
                    self.pump(result.submission_id)
                else:
                    # 请求未接收时显示原因，例如运行时忙碌或正在关闭。
                    self.write(result.reason)
        except (EOFError, KeyboardInterrupt):
            # 输入结束（如 Ctrl+D）或 Ctrl+C 中断时，进入下面的统一收尾。
            pass
        finally:
            # 不论正常退出还是发生异常，都通知运行时停止并保存会话。
            self.runtime.close()
            # 不指定请求 ID，等待显示线程收到 runtime_closed 并设置 self.closed。
            self.pump()
            # 等待显示线程退出，确保关闭事件已处理完。
            self._renderer.join()
            # 恢复普通终端打印模式。
            core.CLI_ACTIVE = False


def run_cli():
    print("agent-harness: agent loop + tools + workflow + goal")
    print("Enter to send; q to quit. /goal, /resume and /delete manage the session.")
    CLIAdapter(InteractiveRuntime()).run()


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "workflow":
        workflow_demo.register_workflows()
        workflow_runtime.RUNNER_FACTORY = lambda: workflow_runtime.SubagentRunner(
            workflow_demo.DemoModelClient(), core.MODEL)
        asyncio.run(workflow_demo.run_demo(argv[1:]))
    elif argv == ["tui"]:
        try:
            from .tui.app import HarnessApp
        except ModuleNotFoundError as exc:
            if exc.name != "textual":
                raise
            raise SystemExit('Textual is required: python -m pip install "textual>=8.2.8,<9"') from None
        result = HarnessApp().run()
        if result:
            raise SystemExit(result)
    else:
        run_cli()


if __name__ == "__main__":
    main()
