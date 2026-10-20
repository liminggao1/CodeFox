# web：Python Web 适配器

本目录把浏览器消息转换成 `InteractiveRuntime` 请求，并把共享运行时事件转发到浏览器。

## 本层职责

- `app.py`：创建 FastAPI 应用，提供 `/api/commands` 元数据接口、`/api/diff` 只读 Git Diff 接口和 `/api/runtime` WebSocket。
- `bridge.py`：管理单个浏览器连接、事件确认、流式事件、工具审批、停止回合和权限模式切换。
- `workspace/diff.py`：按运行时当前项目目录收集 staged 与 unstaged 变更；限制文件和响应大小，并为 Git 错误、二进制文件和过大文件返回结构化状态。

适配器不复制命令注册表、会话存储或工具规则。命令定义来自 `runtime.local_commands`，
会话和权限操作由 `InteractiveRuntime` 执行。浏览器端页面见仓库根目录 [frontend/](../../../../frontend/README.md)。

## 启动

```sh
PYTHONPATH=src python -m uvicorn codefox.frontends.web.app:app --host 127.0.0.1 --port 8010
```

同一时刻只接受一个浏览器连接；连接断开后运行时仍由服务进程管理，浏览器会自动重连。

`GET /api/diff` 只读取 `InteractiveRuntime.session.store.workdir`，不接受浏览器传入的路径。
响应包含当前 Git 分支、提交、文件状态、增删行数和统一 Diff；接口不会调用模型、修改文件或执行提交。
