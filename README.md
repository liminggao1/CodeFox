# agent-harness

s15 集成宿主 + s16 工作流运行时 + s17 目标循环，合并成一个自包含项目。

面向学习调试：代码近原样搬运，保留分段注释和全局状态，不做异步化重构。

## 运行

```sh
# 交互会话（调用真实 API，读 .env）
python cli.py

# 离线演示工作流：mock runner，不联网
python cli.py workflow demo
python cli.py workflow resume

# 单文件教学版，等价入口，可独立运行
python flat/agent_full.py
python flat/agent_full.py workflow demo
```

依赖与配置：`pip install -r requirements.txt`，复制 `.env.example` 为 `.env` 并填好 key。
沿用 conda 环境 `learncc` 即可。

## 目录

| 路径 | 内容 |
| --- | --- |
| `cli.py` | 入口：交互循环 + `/goal` 本地处理 + workflow demo |
| `agent/host.py` | s15 主体：内置工具、团队、cron、MCP、memory、后台、压缩 |
| `agent/memory_runtime.py` | s09 的 memory 运行时，整份搬入 |
| `agent/workflow.py` | s16 的 workflow 运行时（journal / 预算 / resume） |
| `agent/goal.py` | s17 的 GoalController / PromptGoalEvaluator |
| `tools/build_flat.py` | 拼接生成 `flat/agent_full.py` |
| `flat/agent_full.py` | 生成物：全部机制在一个文件里 |
| `tests/` | 56 个离线测试 |
| `skills/` | s07/s15 使用的技能资产 |

## 相对原课程的改动

1. **memory 改为正常导入**：不再按路径加载 `s09_memory/code.py`，改为 `agent/memory_runtime.py`。
2. **workflow 原生化**：删掉 `install_workflow_tool` / `load_integrated_host` 的 monkeypatch，
   `Workflow` 工具直接进 `assemble_tool_pool()`。
3. **goal 接入 Stop 边界**：`agent_loop` 在「模型不再调用工具」时先问目标控制器，
   返回 `block` 就补一条 `[Goal still active]` 消息继续同一条循环。
4. **资源归属**：运行时状态（`.tasks` / `.memory` / `.runtime` / `.mailboxes` /
   `.worktrees` / `.scheduled_tasks.json` / `.transcripts` / `.task_outputs`）跟随
   **启动目录**；`skills/` 跟随**项目目录**。
5. **goal 状态只在内存**：重启后需要重新 `/goal`，与 s17 一致。
6. **跨平台**：`agent/workflow.py` 的 `fcntl` 用 try/except 包裹，无 fcntl 时退化为
   进程内锁并打印一次提示。

## 测试

```sh
python -m pytest -q
```

全部离线，不消耗 API。移植自原仓库的三个测试文件并做了适配：

- `tests/test_goal.py`：控制器用例直接移植；会话级用例改为用脚本化 client 驱动 `agent_loop`。
- `tests/test_workflow.py`：17 个用例，`install/load` 类改为断言真实的工具池与 runner 接线。
- `tests/test_agent_teams.py`：只保留适用于合并宿主的运行时用例，丢弃跨章节路径与对比。
- `tests/test_flat_build.py`：拼接输出确定、与生成物一致、可编译、可运行。

## 重新生成单文件版

改完 `agent/` 下任一模块后：

```sh
python tools/build_flat.py
```

`tests/test_flat_build.py::test_committed_flat_file_is_current` 会在生成物过期时报错。