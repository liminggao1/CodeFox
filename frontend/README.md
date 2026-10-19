# frontend：Web 工作台

本目录是 CodeFox 的 React + Vite 浏览器界面。它通过同源 `/api` 请求连接 Python FastAPI 适配器，
不直接读取会话文件，也不实现模型、工具或命令执行逻辑。

## 本层职责

- `src/App.tsx`：连接 WebSocket，展示对话、工具调用、审批、会话和运行状态；输入区底部显示当前模型与上下文窗口占用，满载时保留 `/compact` 压缩入口并阻止普通消息提交。
- `src/protocol.ts`：定义浏览器与 Web 适配器之间的消息形状和 JSON 安全读取函数。
- `src/styles.css`：工作台布局、响应式样式和状态面板样式。
- `vite.config.ts`：本地开发服务器和 `/api`、WebSocket 代理。
- `package.json`：Node 依赖与开发、构建命令。

后端协议和运行时行为见 [Python Web 适配器](../src/codefox/frontends/web/README.md)。

## 开发

在仓库根目录运行：

```sh
nvm use
npm --prefix frontend ci
npm --prefix frontend run dev
```

生产构建检查：

```sh
npm --prefix frontend run build
```

开发服务器默认是 `http://127.0.0.1:5173`，会把 `/api` 转发到本地 FastAPI 服务 `127.0.0.1:8000`。
