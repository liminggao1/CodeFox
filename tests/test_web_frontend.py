"""Offline tests for the local WebSocket adapter."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from codefox.frontends.web.app import create_app
from codefox.runtime import interaction


class FakeRuntime:
    """Small channel-backed runtime for exercising the adapter without a model."""

    def __init__(self, workdir=None):
        self.channel = interaction.InteractionChannel()
        self.channel.set_projector(self._display_record)
        self.session = SimpleNamespace(
            store=SimpleNamespace(workdir=Path(workdir or Path.cwd()).resolve()),
        )
        self.session_id = uuid.uuid4().hex[:8]
        self.generation = 1
        self.sessions = [{
            "id": self.session_id, "updated_at": "2026-01-01T00:00:00+00:00",
            "summary": "初始会话", "message_count": 0,
        }]
        self.busy = False
        self.permission_mode = "default"
        self.active_turn = None
        self.stop_event = None
        self.lock = threading.Lock()
        self.thread = None
        self.closed = False
        self.approval_started = threading.Event()

    def set_permission_mode(self, mode):
        with self.lock:
            if self.busy or self.closed:
                raise ValueError("当前正在执行，不能切换权限模式")
            if mode not in {"default", "no_approval"}:
                raise ValueError("Unknown permission mode")
            self.permission_mode = mode

    @staticmethod
    def _display_record(event):
        if event.session_id is None:
            return None
        if (event.type == "command_result"
                and "sessions" in event.payload.get("data", {})):
            return None
        return {
            "id": event.id, "at": event.at, "kind": "display",
            "type": event.type, "source": event.source,
            "turn_id": event.turn_id, "payload": event.payload,
        }

    def _context(self, *, turn_id=None, session_id=None, generation=None, stop_event=None):
        return interaction.ExecutionContext(
            self.channel, self.session_id if session_id is None else session_id,
            self.generation if generation is None else generation,
            turn_id=turn_id, origin="frontend", stop_event=stop_event,
            checkpoint=lambda **kwargs: True,
        )

    def start(self):
        self.channel.publish(
            self._context(), "state_snapshot", {"busy": False, "session_id": self.session_id},
        )

    def submit(self, text):
        if not isinstance(text, str) or not text.strip():
            return interaction.SubmissionResult("invalid", reason="Empty input")
        with self.lock:
            if self.closed:
                return interaction.SubmissionResult("closed", reason="Runtime is closing")
            if self.busy:
                return interaction.SubmissionResult("busy", reason="A turn or command is running")
            self.busy = True
            submission_id = uuid.uuid4().hex
            command = text.strip().split()[0].startswith("/")
            turn_id = None if command else uuid.uuid4().hex
            self.active_turn = turn_id
            self.stop_event = threading.Event() if turn_id else None
            self.thread = threading.Thread(
                target=self._run_command if command else self._run_turn,
                args=(text.strip(), submission_id, turn_id),
                daemon=True,
            )
            self.thread.start()
        return interaction.SubmissionResult(
            "accepted", submission_id, "command" if command else "turn",
        )

    def _finish_command(self, submission_id, command, data, *, session_id=None,
                        generation=None):
        self.channel.publish(
            self._context(session_id=session_id, generation=generation),
            "command_result", {
                "submission_id": submission_id, "command": command,
                "status": "completed", "data": data,
            },
        )
        with self.lock:
            self.busy = False
            self.active_turn = None

    def _run_command(self, text, submission_id, _turn_id):
        parts = text.split()
        command = parts[0]
        if command == "/resume" and len(parts) == 1:
            self._finish_command(submission_id, command, {
                "sessions": self.sessions, "errors": [],
            })
            return
        if command == "/new":
            self.session_id = uuid.uuid4().hex[:8]
            self.generation += 1
            self.sessions.insert(0, {
                "id": self.session_id, "updated_at": "2026-01-02T00:00:00+00:00",
                "summary": "", "message_count": 0,
            })
            context = self._context()
            context.publish("session_changed", records=[])
            self._finish_command(submission_id, command, {"text": "已开启新会话"})
            return
        if command == "/resume" and len(parts) == 2:
            self.session_id = parts[1]
            self.generation += 1
            context = self._context()
            context.publish("session_changed", records=[])
            self._finish_command(submission_id, command, {"text": "已切换会话"})
            return
        self._finish_command(submission_id, command, {"error": "未知命令"})

    def _run_turn(self, text, submission_id, turn_id):
        stop_event = self.stop_event
        context = self._context(turn_id=turn_id, stop_event=stop_event)
        context.publish("user_message", text=text, submission_id=submission_id)
        context.publish("turn_started", submission_id=submission_id)
        if text == "slow":
            stopped = stop_event.wait(3)
            status = "stopped" if stopped else "completed"
            answer = "已停止" if stopped else "完成"
        elif text == "approval":
            self.approval_started.set()
            decision = self.channel.request_approval(
                context, "bash", "tool-1", {"command": "printf test"},
            )
            if decision == "approve":
                context.publish("tool_call", id="tool-1", name="bash",
                                input={"command": "printf test"})
                context.publish("tool_result", tool_use_id="tool-1",
                                content="test", status="success")
                answer = "工具已批准"
                status = "completed"
            else:
                answer = "工具已拒绝"
                status = "completed"
        else:
            context.publish("assistant_text_delta", model_call_id="call-1",
                            block_index=0, sequence=1, delta="流式 ")
            context.publish("assistant_text_delta", model_call_id="call-1",
                            block_index=0, sequence=2, delta="回复")
            answer = "流式 回复"
            status = "completed"
        context.publish("assistant_message", text=answer, model_call_id="call-1")
        context.publish("turn_finished", outcome={"status": status, "reason": ""},
                        summary={"elapsed_seconds": 0, "tool_calls": 0,
                                 "approvals": 0, "lead_usage": {
                                     "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                 }})
        with self.lock:
            self.busy = False
            self.active_turn = None

    def request_stop(self, turn_id):
        with self.lock:
            if turn_id != self.active_turn or self.stop_event is None:
                return False
            self.stop_event.set()
            return True

    def close(self):
        with self.lock:
            self.closed = True
            if self.stop_event is not None:
                self.stop_event.set()
        self.channel.close_approvals()
        if self.thread is not None:
            self.thread.join(timeout=4)
        self.channel.publish(self._context(), "runtime_closed", {"saved": True})
        self.channel.close()


@pytest.fixture
def runtime():
    return FakeRuntime()


def connect(socket):
    initial = socket.receive_json()
    assert initial["type"] == "event"
    assert initial["event"]["type"] == "state_snapshot"
    socket.send_json({"type": "ack", "sequence": initial["event"]["sequence"]})


def receive_event(socket, *, acknowledge=True):
    while True:
        message = socket.receive_json()
        if message["type"] != "event":
            continue
        event = message["event"]
        if acknowledge:
            socket.send_json({"type": "ack", "sequence": event["sequence"]})
        return event


def receive_message(socket, kind):
    message, _ = receive_until_message(socket, kind)
    return message


def receive_until_message(socket, kind):
    events = []
    while True:
        message = socket.receive_json()
        if message["type"] == "event":
            event = message["event"]
            socket.send_json({"type": "ack", "sequence": event["sequence"]})
            events.append(event)
        elif message["type"] == kind:
            return message, events


def receive_event_type(socket, event_type):
    while True:
        event = receive_event(socket)
        if event["type"] == event_type:
            return event


def test_websocket_replays_unacknowledged_event_and_rejects_second_client(runtime):
    client = TestClient(create_app(lambda: runtime))
    with client:
        with client.websocket_connect("/api/runtime") as first:
            original = first.receive_json()["event"]
            with client.websocket_connect("/api/runtime") as second:
                rejected = second.receive_json()
                assert rejected["type"] == "connection_rejected"
        with client.websocket_connect("/api/runtime") as reconnected:
            replay = reconnected.receive_json()["event"]
            assert replay["id"] == original["id"]
            assert replay["sequence"] == original["sequence"]
            reconnected.send_json({"type": "ack", "sequence": replay["sequence"]})


def test_command_metadata_comes_from_shared_registry(runtime):
    with TestClient(create_app(lambda: runtime)) as client:
        response = client.get("/api/commands")
    assert response.status_code == 200
    commands = response.json()["commands"]
    assert [command["name"] for command in commands] == [
        "/goal", "/compact", "/new", "/resume", "/delete", "/quit",
    ]
    assert commands[4]["requires_argument"] is True


def test_diff_endpoint_reads_runtime_workdir_without_accepting_client_path(tmp_path):
    from tests.test_workspace_diff import init_repo

    init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("before\nchanged\nend\n", encoding="utf-8")
    runtime = FakeRuntime(tmp_path)
    with TestClient(create_app(lambda: runtime)) as client:
        response = client.get("/api/diff?path=../outside")

    assert response.status_code == 200
    payload = response.json()
    assert payload["workdir"]["name"] == tmp_path.name
    assert [item["path"] for item in payload["files"]] == ["tracked.txt"]
    assert "+changed" in payload["files"][0]["patch"]


def test_diff_endpoint_returns_structured_non_git_error(tmp_path):
    runtime = FakeRuntime(tmp_path)
    with TestClient(create_app(lambda: runtime)) as client:
        response = client.get("/api/diff")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_git_repository"


def test_permission_mode_message_updates_runtime_and_reports_failures(runtime):
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as socket:
            connect(socket)
            socket.send_json({"type": "permission_mode", "mode": "no_approval"})
            changed = receive_message(socket, "action_result")
            assert changed["action"] == "permission_mode"
            assert changed["accepted"] is True
            assert changed["mode"] == "no_approval"
            assert runtime.permission_mode == "no_approval"

            socket.send_json({"type": "permission_mode", "mode": "invalid"})
            rejected = receive_message(socket, "action_result")
            assert rejected["accepted"] is False
            assert "Unknown permission mode" in rejected["reason"]


def test_submit_stream_invalid_busy_and_stop(runtime):
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as socket:
            connect(socket)
            socket.send_json({"type": "submit", "text": "hello"})
            accepted, events = receive_until_message(socket, "submission_result")
            assert accepted["result"]["status"] == "accepted"
            delta = next((event for event in events
                          if event["type"] == "assistant_text_delta"), None)
            if delta is None:
                delta = receive_event_type(socket, "assistant_text_delta")
            assert delta["payload"]["delta"] == "流式 "
            finished = next((event for event in events
                             if event["type"] == "turn_finished"), None)
            if finished is None:
                finished = receive_event_type(socket, "turn_finished")
            assert finished["payload"]["outcome"]["status"] == "completed"

            socket.send_json({"type": "submit", "text": ""})
            invalid = receive_message(socket, "submission_result")
            assert invalid["result"]["status"] == "invalid"

            socket.send_json({"type": "submit", "text": "slow"})
            slow_accepted, events = receive_until_message(socket, "submission_result")
            assert slow_accepted["result"]["status"] == "accepted"
            started = next((event for event in events
                            if event["type"] == "turn_started"), None)
            if started is None:
                started = receive_event_type(socket, "turn_started")
            socket.send_json({"type": "submit", "text": "busy"})
            busy = receive_message(socket, "submission_result")
            assert busy["result"]["status"] == "busy"
            socket.send_json({"type": "stop", "turn_id": started["turn_id"]})
            stopped, events = receive_until_message(socket, "action_result")
            assert stopped["accepted"] is True
            finished = next((event for event in events if event["type"] == "turn_finished"), None)
            if finished is None:
                finished = receive_event_type(socket, "turn_finished")
            assert finished["payload"]["outcome"]["status"] == "stopped"


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_approval_waits_for_event_ack_and_decision_is_forwarded(runtime, decision):
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as socket:
            connect(socket)
            socket.send_json({"type": "submit", "text": "approval"})
            first = None
            while first is None:
                message = socket.receive_json()
                if message["type"] == "submission_result":
                    assert message["result"]["status"] == "accepted"
                elif message["type"] == "event":
                    first = message["event"]
            assert first["type"] == "user_message"
            assert runtime.approval_started.wait(timeout=2)

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with runtime.channel._changed:
                    if runtime.channel._requests:
                        break
                time.sleep(0.01)
            assert runtime.channel.next_approval(timeout=0) is None

            socket.send_json({"type": "ack", "sequence": first["sequence"] + 100})
            with runtime.channel._changed:
                assert first["sequence"] in runtime.channel._unacknowledged
            socket.send_json({"type": "ack", "sequence": first["sequence"]})

            request_message = receive_message(socket, "approval_request")
            request = request_message["request"]
            socket.send_json({
                "type": "approval_decision", "request_id": request["request_id"],
                "generation": True, "decision": [],
            })
            invalid, _ = receive_until_message(socket, "action_result")
            assert invalid["accepted"] is False
            socket.send_json({
                "type": "approval_decision", "request_id": request["request_id"],
                "generation": request["generation"], "decision": decision,
            })
            accepted, events = receive_until_message(socket, "action_result")
            assert accepted["accepted"] is True
            finished = next((event for event in events if event["type"] == "turn_finished"), None)
            if finished is None:
                finished = receive_event_type(socket, "turn_finished")
            assert finished["payload"]["outcome"]["status"] == "completed"


def test_approval_request_is_replayed_after_websocket_reconnect(runtime):
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as first:
            connect(first)
            first.send_json({"type": "submit", "text": "approval"})
            original, _ = receive_until_message(first, "approval_request")
            request_id = original["request"]["request_id"]

        with client.websocket_connect("/api/runtime") as reconnected:
            replay, _ = receive_until_message(reconnected, "approval_request")
            request = replay["request"]
            assert request["request_id"] == request_id
            reconnected.send_json({
                "type": "approval_decision", "request_id": request["request_id"],
                "generation": request["generation"], "decision": "deny",
            })
            result, events = receive_until_message(reconnected, "action_result")
            assert result["accepted"] is True
            finished = next((event for event in events if event["type"] == "turn_finished"), None)
            if finished is None:
                finished = receive_event_type(reconnected, "turn_finished")
            assert finished["payload"]["outcome"]["status"] == "completed"


def test_websocket_runs_interactive_runtime_with_offline_model(runtime_factory):
    from support.fakes import response

    runtime, _ = runtime_factory([response("Web 工作台已连接")])
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as socket:
            socket.send_json({"type": "submit", "text": "请回复测试消息"})
            accepted, events = receive_until_message(socket, "submission_result")
            assert accepted["result"]["status"] == "accepted"

            finished = next((event for event in events if event["type"] == "turn_finished"), None)
            while finished is None:
                event = receive_event(socket)
                events.append(event)
                if event["type"] == "turn_finished":
                    finished = event

            assert finished["payload"]["outcome"]["status"] == "completed"
            replies = [
                event["payload"]["record"]["payload"]["text"]
                for event in events
                if event["type"] == "display_record"
                and event["payload"]["record"]["type"] == "assistant_message"
            ]
            assert replies == ["Web 工作台已连接"]


def test_session_list_new_and_resume_use_runtime_commands(runtime):
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as socket:
            connect(socket)
            socket.send_json({"type": "submit", "text": "/resume"})
            listed = receive_event_type(socket, "command_result")
            entries = listed["payload"]["data"]["sessions"]
            assert entries[0]["id"] == runtime.session_id

            socket.send_json({"type": "submit", "text": "/new"})
            changed = receive_event_type(socket, "session_changed")
            new_id = changed["session_id"]
            assert new_id != entries[0]["id"]
            assert changed["payload"]["records"] == []
            receive_event_type(socket, "command_result")

            socket.send_json({"type": "submit", "text": "/resume " + entries[0]["id"]})
            restored = receive_event_type(socket, "session_changed")
            assert restored["session_id"] == entries[0]["id"]
            receive_event_type(socket, "command_result")


def test_session_generation_change_discards_stale_in_flight_event(runtime):
    with TestClient(create_app(lambda: runtime)) as client:
        with client.websocket_connect("/api/runtime") as socket:
            stale = socket.receive_json()["event"]
            assert stale["type"] == "state_snapshot"
            socket.send_json({"type": "submit", "text": "/resume " + runtime.session_id})
            accepted = receive_message(socket, "submission_result")
            assert accepted["result"]["status"] == "accepted"

            restored = receive_event_type(socket, "session_changed")
            assert restored["session_id"] == runtime.session_id
            assert restored["sequence"] > stale["sequence"]
            receive_event_type(socket, "command_result")


def test_shutdown_closes_runtime_and_drains_notifications(runtime):
    client = TestClient(create_app(lambda: runtime))
    with client:
        with client.websocket_connect("/api/runtime"):
            pass
    assert runtime.closed
    assert runtime.channel._closed
    assert not runtime.channel._unacknowledged
