"""FastAPI application factory for the local Web workbench."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Callable

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from ...runtime.interactive_runtime import InteractiveRuntime
from ...runtime.local_commands import LOCAL_COMMANDS
from ...workspace.diff import DiffError, collect_diff
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

    @app.get("/api/diff", response_model=None)
    def diff_snapshot() -> dict | JSONResponse:
        """读取后端运行时当前项目的 Git 变更，供 Web Diff 工作区展示。"""
        bridge: RuntimeBridge | None = getattr(app.state, "runtime_bridge", None)
        if bridge is None:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "runtime_unavailable", "message": "运行时尚未启动"}},
            )
        try:
            return collect_diff(bridge.runtime.session.store.workdir)
        except DiffError as exc:
            status_code = 503 if exc.code in {"not_git_repository", "git_command_failed"} else 400
            return JSONResponse(
                status_code=status_code,
                content={"error": {"code": exc.code, "message": exc.message}},
            )

    @app.websocket("/api/runtime")
    async def runtime_socket(websocket: WebSocket) -> None:
        """把浏览器的 WebSocket 连接交给运行时桥接器处理。"""
        bridge: RuntimeBridge = websocket.app.state.runtime_bridge
        await bridge.serve(websocket)

    return app


app = create_app()
