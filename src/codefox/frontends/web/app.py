"""FastAPI application factory for the local Web workbench."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Callable

from fastapi import FastAPI, WebSocket

from ...runtime.interactive_runtime import InteractiveRuntime
from ...runtime.local_commands import LOCAL_COMMANDS
from .bridge import RuntimeBridge


def create_app(runtime_factory: Callable[[], InteractiveRuntime] = InteractiveRuntime) -> FastAPI:
    """创建本地 Web 适配器；测试可以注入离线运行时工厂。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """在 FastAPI 启动时创建共享运行时，在关闭时释放它。"""
        bridge = RuntimeBridge(runtime_factory())
        app.state.runtime_bridge = bridge
        try:
            bridge.start()
            yield
        finally:
            await bridge.shutdown()

    app = FastAPI(title="CodeFox", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """提供最小健康检查，确认 FastAPI 进程正在监听。"""
        return {"status": "ok"}

    @app.get("/api/commands")
    def commands() -> dict[str, list[dict[str, object]]]:
        """返回运行时注册的命令元数据，供浏览器输入补全使用。"""
        return {"commands": [asdict(command) for command in LOCAL_COMMANDS]}

    @app.websocket("/api/runtime")
    async def runtime_socket(websocket: WebSocket) -> None:
        """把浏览器的 WebSocket 连接交给运行时桥接器处理。"""
        bridge: RuntimeBridge = websocket.app.state.runtime_bridge
        await bridge.serve(websocket)

    return app


app = create_app()
