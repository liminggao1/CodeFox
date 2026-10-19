# web：Python Web 适配器

本目录把浏览器消息转换成 `InteractiveRuntime` 请求，并把共享运行时事件转发到浏览器。

## 本层职责

- `app.py`：创建 FastAPI 应用，提供 `/api/commands` 元数据接口和 `/api/runtime` WebSocket。
- `bridge.py`：管理单个浏览器连接、事件确认、流式事件、工具审批、停止回合和权限模式切换。

适配器不复制命令注册表、会话存储或工具规则。命令定义来自 `runtime.local_commands`，
会话和权限操作由 `InteractiveRuntime` 执行。浏览器端页面见仓库根目录 [frontend/](../../../../frontend/README.md)。

## 启动

```sh
PYTHONPATH=src python -m uvicorn codefox.frontends.web.app:app --host 127.0.0.1 --port 8000
```

同一时刻只接受一个浏览器连接；连接断开后运行时仍由服务进程管理，浏览器会自动重连。
