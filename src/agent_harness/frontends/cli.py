"""Command routing and the sole stdin reader for the interactive CLI."""

from __future__ import annotations

import asyncio
import json
import sys
import threading

from .. import core
from ..workflows import workflow_runtime, workflow_demo
from ..runtime.interactive_runtime import InteractiveRuntime


class CLIAdapter:
    def __init__(self, runtime, reader=None, write=None):
        self.runtime = runtime
        self.reader = reader or core.CONSOLE.ask
        self.write = write or core.terminal_print
        self.closed = False
        self._completed = set()
        self._changed = threading.Event()
        self._renderer = None

    def render(self, event):
        payload = event.payload
        if event.type in {"assistant_message", "log"}:
            self.write(payload["text"])
        elif event.type == "tool_call":
            self.write(f"> {payload['name']}")
        elif event.type == "tool_result":
            self.write(str(payload["content"])[:300])
        elif event.type == "command_result":
            data = payload["data"]
            for entry in data.get("sessions", []):
                self.write(f"{entry['id']}  {entry['updated_at']}  {entry['message_count']} messages  {entry['summary']}")
            for error in data.get("errors", []):
                self.write(f"[session error] {error}")
            if "sessions" in data and not data["sessions"]:
                self.write("No saved sessions")
            if data.get("text") or data.get("error"):
                self.write(data.get("text") or data["error"])
        elif event.type in {"runtime_error", "session_save_error"}:
            self.write(payload["error"])
        elif event.type == "turn_finished" and payload["outcome"]["status"] != "completed":
            self.write(f"[{payload['outcome']['status']}] {payload['outcome']['reason']}")
        elif event.type == "runtime_closed":
            self.closed = True
            if not payload["saved"]:
                self.write("Session could not be saved")

    def pump(self, submission_id=None):
        """Wait for accepted work while collecting approvals on this same thread."""
        while not self.closed:
            request = self.runtime.channel.next_approval()
            if request is not None:
                self.write(f"[permission] {request.source}: {request.tool_name}")
                self.write(json.dumps(request.arguments, ensure_ascii=False, indent=2))
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
        core.CLI_ACTIVE = True
        self.runtime.start()
        self._renderer = threading.Thread(target=self._render_events, name="cli-output", daemon=True)
        self._renderer.start()
        try:
            while not self.closed:
                text = self.reader()
                if text.strip().lower() in {"", "q", "exit", "/quit"}:
                    break
                result = self.runtime.submit(text)
                if result.status == "accepted":
                    self.pump(result.submission_id)
                else:
                    self.write(result.reason)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self.runtime.close()
            self.pump()
            self._renderer.join()
            core.CLI_ACTIVE = False


def run_cli():
    print("agent-harness: agent loop + tools + workflow + goal")
    print("Enter to send; q to quit. /goal, /resume and /delete manage the session.")
    CLIAdapter(InteractiveRuntime()).run()


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "workflow":
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
