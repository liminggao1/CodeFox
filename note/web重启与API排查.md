# Web 工作台重启与 API 排查

本笔记用于本项目本地 Web 工作台。项目运行在 WSL2 的 `learncc` conda 环境中，后端和前端需要分别占用一个终端。

## 1. 停止当前服务

分别切换到运行后端和前端的终端，按 `Ctrl+C`。

修改 `.env` 后必须重启后端，因为环境变量是在进程启动时读取的。只修改前端代码时，Vite 通常会自动刷新，不需要重启前端。

如果原来的终端已经关闭，但启动时提示端口被占用，先在 WSL 中查看占用进程：

```sh
ss -ltnp | grep -E ':(8010|5173)\b'
```

输出中的 `pid=数字` 就是进程号。确认端口对应的是本项目旧进程后，执行：

```sh
kill <PID>
```

例如：

```sh
kill 12345
```

再次确认端口已经释放：

```sh
ss -ltnp | grep -E ':(8010|5173)\b'
```

没有输出后，再执行下面的“启动后端”和“启动前端”步骤。不要直接按进程名批量关闭，以免误停其他项目的服务。

## 2. 启动后端

打开一个 WSL 终端，执行：

```sh
conda activate learncc
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
PYTHONPATH=src python -m uvicorn codefox.frontends.web.app:app --host 127.0.0.1 --port 8010
```

看到类似下面的内容，说明 Web 后端已经监听：

```text
Uvicorn running on http://127.0.0.1:8010
```

这个终端需要保持运行，不要关闭。

## 3. 启动前端

再打开一个 WSL 终端，执行：

```sh
conda activate learncc
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
npm --prefix frontend run dev
```

看到类似下面的地址后，在浏览器打开它：

```text
http://127.0.0.1:5173
```

这个终端也需要保持运行。

## 4. 快速判断问题在哪一层

### 浏览器页面打不开

先确认前端终端仍在运行，并访问：

```text
http://127.0.0.1:5173
```

如果 5173 页面打不开，问题在前端开发服务器或 Node 环境。

### 页面能打开，但请求失败

先确认后端终端仍在运行。后端启动失败时，浏览器通常只能显示请求失败。

可以在 WSL 中检查 8010 端口：

```sh
curl -i http://127.0.0.1:8010/api/health
```

返回 `200` 和 `{"status":"ok"}` 说明后端已经连通；如果出现 `Connection refused`，说明后端没有启动或端口不是 8010。

### 页面显示 `SSL: UNEXPECTED_EOF_WHILE_READING`

这表示后端访问模型服务时 HTTPS/TLS 连接被提前关闭。它通常不是 React 页面错误，也不是 API Key 错误。

可以先重试一次。如果持续出现，检查后端终端里的完整错误，并从同一个 WSL 环境测试接口：

```sh
curl -i --connect-timeout 10 --max-time 20 \
  https://flxapi.cc/v1/models
```

不带 API Key 时返回 `401 API_KEY_REQUIRED` 是正常的，说明域名和 HTTPS 可以访问。若出现连接超时、TLS 错误或无法解析域名，再检查网络、代理或 VPN。

## 5. 修改 `.env` 后的固定流程

1. 保存 `.env`。
2. 在后端终端按 `Ctrl+C`。
3. 重新执行“启动后端”中的命令。
4. 保持前端终端运行，刷新 `http://127.0.0.1:5173`。
5. 新建一个会话后重新发送请求。

## 6. 常见端口问题

如果启动时提示 8010 或 5173 已被占用，先回到之前启动服务的终端按 `Ctrl+C`，再重新启动。不要在多个终端同时启动同一个后端实例。

本项目的标准端口是：

| 服务 | 地址 |
| --- | --- |
| Web 前端 | `127.0.0.1:5173` |
| Web 后端 | `127.0.0.1:8010` |
| 模型 API | `.env` 中的 `ANTHROPIC_BASE_URL` |

## 7. 前后端调试

### 7.1 调试前端

前端使用浏览器开发者工具调试。打开 `http://127.0.0.1:5173` 后按 `F12`：

1. 在 `Sources` 中打开 `frontend/src/App.tsx`。
2. 在 `connect()` 处观察 WebSocket 是否建立。
3. 在 `submit()` 处观察是否发送了用户输入。
4. 在 `socket.onmessage` 或 `applyEvent()` 处观察后端返回的事件。
5. 在 `Network` 面板中点击 `/api/runtime`，查看 WebSocket 的收发消息。

输入“你好”时，前端应发送类似下面的消息：

```json
{"type":"submit","text":"你好"}
```

如果页面能打开但没有这条消息，问题在 React 输入处理或 WebSocket 连接；如果消息已经发出但没有返回，继续调试后端。

### 7.2 调试后端

后端使用 VS Code 的 **Remote-WSL** 窗口调试。不要在 Windows Python 中启动，也不要在 VS Code 调试配置中添加 `--reload`，否则 Uvicorn 子进程可能导致断点不稳定。

在 `.vscode/launch.json` 的 `configurations` 中增加一个 FastAPI 配置：

```json
{
  "name": "Web 后端 FastAPI",
  "type": "debugpy",
  "request": "launch",
  "module": "uvicorn",
  "args": [
    "codefox.frontends.web.app:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8010"
  ],
  "cwd": "${workspaceFolder}",
  "console": "integratedTerminal",
  "justMyCode": true,
  "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
  "pythonArgs": ["-Xfrozen_modules=off"],
  "env": {
    "PYTHONPATH": "${workspaceFolder}/src"
  }
}
```

启动顺序：

1. 在 VS Code 中按 `F5`，选择“Web 后端 FastAPI”。
2. 另开一个 WSL 终端启动前端。使用 Node 24 时先执行：

   ```sh
   . ~/.nvm/nvm.sh
   nvm use 24.22.0
   npm --prefix frontend run dev
   ```

3. 浏览器打开 `http://127.0.0.1:5173`，输入“你好”。

### 7.3 一次“你好”请求的后端断点顺序

按下面顺序设置断点，可以看到一条请求从浏览器走到模型再返回：

| 顺序 | 文件 | 观察内容 |
| --- | --- | --- |
| 1 | `src/codefox/frontends/web/app.py` | FastAPI 是否接收到 WebSocket 连接 |
| 2 | `src/codefox/frontends/web/bridge.py` | 是否收到 `submit` 消息 |
| 3 | `src/codefox/runtime/interactive_runtime.py` | 输入是否成功排队并唤醒运行时线程 |
| 4 | `src/codefox/runtime/turn_runner.py` | 是否开始执行一次用户回合 |
| 5 | `src/codefox/host.py` | 是否进入 Agent 主循环和模型调用入口 |
| 6 | `src/codefox/agents/turn_engine.py` | 请求是否准备完成、是否收到模型响应 |
| 7 | `src/codefox/providers/llm_client.py` | 实际 API 地址、HTTPS 请求和响应内容 |
| 8 | `frontend/src/App.tsx` | 前端是否收到事件并渲染助手回复 |

常用操作：`F10` 单步跳过、`F11` 进入函数、`Shift+F11` 返回、`F5` 继续、`Shift+F5` 停止调试。

### 7.4 不消耗 API 的调试

要先熟悉 Agent 主循环，可以使用现有的离线配置：

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
/home/gaogao/miniconda3/envs/learncc/bin/python scripts/debug_pass.py
```

VS Code 中也可以选择 `.vscode/launch.json` 的“端到端自检（离线）”或“Agent 交互（模拟 API，离线）”。
