"""Translate one browser WebSocket into the shared runtime channel."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from anyio import BrokenResourceError, ClosedResourceError
from fastapi import WebSocket, WebSocketDisconnect


class RuntimeBridge:
    """Own the web adapter lifecycle and preserve unacknowledged delivery."""

    def __init__(self, runtime):
        self.runtime = runtime
        self._active_socket: WebSocket | None = None
        self._active_token = 0
        self._next_token = 0
        self._connection_lock = asyncio.Lock()
        self._connection_done = asyncio.Event()
        self._connection_done.set()
        self._send_lock: asyncio.Lock | None = None
        self._event_task: asyncio.Task | None = None
        self._pending_event = None
        self._pending_event_token: int | None = None
        self._pending_approval = None
        self._pending_approval_token: int | None = None
        self._closing = False
        self._started = False

    def start(self) -> None:
        """启动共享交互运行时；同一个后端进程只创建这一份运行时。"""
        self.runtime.start()
        self._started = True

    async def serve(self, websocket: WebSocket) -> None:
        """接管一个浏览器连接，并在连接结束时清理当前连接状态。"""
        async with self._connection_lock:
            if self._active_socket is not None or self._closing:
                await websocket.accept()
                await websocket.send_json({
                    "type": "connection_rejected",
                    "reason": "已有浏览器连接或运行时正在关闭",
                })
                await websocket.close(code=1008)
                return
            await websocket.accept()
            self._next_token += 1
            token = self._next_token
            self._active_token = token
            self._active_socket = websocket
            self._send_lock = asyncio.Lock()
            self._connection_done.clear()

        # 单独的发送任务持续消费运行时事件，避免接收浏览器消息时漏掉模型输出。
        if self._event_task is None:
            self._event_task = asyncio.create_task(self._send_events())
        try:
            await self._receive_messages(websocket, token, self._send_lock)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._connection_lock:
                if self._active_token == token:
                    self._active_socket = None
                    self._active_token = 0
                    self._send_lock = None
                    if self._pending_event is not None:
                        self._pending_event_token = None
                    if self._pending_approval is not None:
                        self._pending_approval_token = None
                    self._connection_done.set()

    async def _send_json(self, websocket: WebSocket, lock: asyncio.Lock,
                         message: dict[str, Any]) -> None:
        """串行发送 JSON，避免事件线程和请求处理同时写入同一条连接。"""
        async with lock:
            await websocket.send_json(message)

    async def _send_events(self) -> None:
        """持续读取运行时事件，并把模型输出或审批请求推送给浏览器。"""
        channel = self.runtime.channel
        while not self._closing:
            websocket = self._active_socket
            token = self._active_token
            send_lock = self._send_lock
            if websocket is None or send_lock is None:
                await asyncio.sleep(0.05)
                continue

            if self._pending_event is None:
                # channel 是同步队列；放到线程中读取，避免阻塞 FastAPI 事件循环。
                event = await asyncio.to_thread(channel.next_event, 0.05)
                if event is not None:
                    self._pending_event = event
                    self._pending_event_token = None

            if self._pending_event is not None:
                if not channel.has_unacknowledged_event(self._pending_event.sequence):
                    self._pending_event = None
                    self._pending_event_token = None
                    continue
                if self._pending_event_token != token:
                    try:
                        await self._send_json(websocket, send_lock, {
                            "type": "event",
                            "event": asdict(self._pending_event),
                        })
                    except (RuntimeError, OSError, WebSocketDisconnect,
                            BrokenResourceError, ClosedResourceError):
                        try:
                            await websocket.close()
                        except (RuntimeError, OSError, WebSocketDisconnect,
                                BrokenResourceError, ClosedResourceError):
                            pass
                    else:
                        self._pending_event_token = token
                await asyncio.sleep(0.02)
                continue

            if self._pending_approval is None:
                request = await asyncio.to_thread(channel.next_approval, 0.05)
                if request is not None:
                    self._pending_approval = request
                    self._pending_approval_token = None

            if (self._pending_approval is not None
                    and self._pending_approval_token != token):
                try:
                    await self._send_json(websocket, send_lock, {
                        "type": "approval_request",
                        "request": asdict(self._pending_approval),
                    })
                except (RuntimeError, OSError, WebSocketDisconnect,
                        BrokenResourceError, ClosedResourceError):
                    try:
                        await websocket.close()
                    except (RuntimeError, OSError, WebSocketDisconnect,
                            BrokenResourceError, ClosedResourceError):
                        pass
                else:
                    self._pending_approval_token = token
            else:
                await asyncio.sleep(0.02)

    async def _receive_messages(self, websocket: WebSocket, token: int,
                                send_lock: asyncio.Lock | None) -> None:
        """接收前端 JSON 命令，并转成运行时提交、停止或审批操作。"""
        if send_lock is None:
            return
        while self._active_token == token:
            try:
                # receive_json 已经完成 JSON 解码，后面只处理协议层字段。
                message = await websocket.receive_json()
            except (ValueError, UnicodeDecodeError):
                await self._send_json(websocket, send_lock, {
                    "type": "protocol_error", "reason": "消息不是有效 JSON",
                })
                continue
            if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                await self._send_json(websocket, send_lock, {
                    "type": "protocol_error", "reason": "消息必须包含字符串 type 字段",
                })
                continue

            kind = message["type"]
            if kind == "submit":
                # submit 只负责排队，真正的 Agent 回合由运行时后台线程执行。
                result = self.runtime.submit(message.get("text"))
                await self._send_json(websocket, send_lock, {
                    "type": "submission_result", "result": asdict(result),
                })
            elif kind == "stop":
                turn_id = message.get("turn_id")
                accepted = (self.runtime.request_stop(turn_id)
                            if isinstance(turn_id, str) else False)
                await self._send_json(websocket, send_lock, {
                    "type": "action_result", "action": "stop",
                    "accepted": accepted,
                })
            elif kind == "ack":
                sequence = message.get("sequence")
                event = self._pending_event
                # 只有当前待确认事件的 sequence 匹配时，才确认并移除它。
                accepted = (type(sequence) is int and event is not None
                            and event.sequence == sequence
                            and self.runtime.channel.acknowledge_event(sequence))
                invalidated = (event is not None and event.sequence == sequence
                               and not self.runtime.channel.has_unacknowledged_event(sequence))
                if accepted or invalidated:
                    self._pending_event = None
                    self._pending_event_token = None
            elif kind == "approval_decision":
                request = self._pending_approval
                decision = message.get("decision")
                matches = (request is not None
                           and message.get("request_id") == request.request_id
                           and type(message.get("generation")) is int
                           and message["generation"] == request.generation
                           and isinstance(decision, str)
                           and decision in {"approve", "deny"})
                accepted = False
                if matches:
                    accepted = self.runtime.channel.respond(
                        request.request_id, request.generation, decision)
                    self._pending_approval = None
                    self._pending_approval_token = None
                await self._send_json(websocket, send_lock, {
                    "type": "action_result", "action": "approval_decision",
                    "accepted": accepted,
                })
            elif kind == "permission_mode":
                mode = message.get("mode")
                accepted = False
                reason = ""
                try:
                    if not isinstance(mode, str):
                        raise ValueError("无效的权限模式")
                    self.runtime.set_permission_mode(mode)
                    accepted = True
                except (OSError, ValueError) as exc:
                    reason = str(exc)
                await self._send_json(websocket, send_lock, {
                    "type": "action_result", "action": "permission_mode",
                    "accepted": accepted, "mode": mode if accepted else None,
                    "reason": reason or None,
                })
            else:
                await self._send_json(websocket, send_lock, {
                    "type": "protocol_error", "reason": f"未知消息类型：{kind}",
                })

    async def shutdown(self) -> None:
        """关闭运行时、WebSocket 和事件任务，并等待后台资源结算完成。"""
        self._closing = True
        if self._started:
            self.runtime.close()
            active_socket = self._active_socket
            if active_socket is not None:
                try:
                    await active_socket.close(code=1001, reason="服务器正在关闭")
                except (RuntimeError, WebSocketDisconnect,
                        BrokenResourceError, ClosedResourceError):
                    pass
            await self._connection_done.wait()
            if self._event_task is not None:
                await self._event_task
                self._event_task = None
            await asyncio.to_thread(self._drain_until_closed)
            self._started = False

    def _drain_until_closed(self) -> None:
        """进程退出时确认剩余通知，直到收到运行时关闭事件。"""
        channel = self.runtime.channel
        if self._pending_event is not None:
            channel.acknowledge_event(self._pending_event.sequence)
            self._pending_event = None
            self._pending_event_token = None
        while True:
            event = channel.next_event(timeout=0.1)
            if event is None:
                continue
            channel.acknowledge_event(event.sequence)
            if event.type == "runtime_closed":
                return
