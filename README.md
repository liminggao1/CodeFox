# agent-harness

本地 code agent 运行环境，打包为标准 Python 项目：`src/` 布局、`pyproject.toml`、console 入口。

## 安装

完整步骤（含换机器、全新克隆）见 [note/env-setup.md](note/env-setup.md)。快速版：

```sh
pip install -e ".[dev]"      # 含测试依赖
cp .env.example .env         # 填好 key
```

## 运行

```sh
# 交互会话（调用真实 API，读 .env）
agent-harness

# 离线演示工作流：mock runner，不联网
agent-harness workflow demo
agent-harness workflow resume

# 单文件版，等价入口，可独立运行
python flat/agent_full.py workflow demo
```

未安装成包时改用 `python -m agent_harness`，并先把源码目录放进 `PYTHONPATH`：

```sh
export PYTHONPATH=src        # Windows PowerShell: $env:PYTHONPATH="src"
python -m agent_harness
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/agent_harness/cli.py` | 入口：交互循环 + `/goal` 本地处理 + workflow demo |
| `src/agent_harness/__main__.py` | `python -m agent_harness` 入口 |
| `src/agent_harness/llm_client.py` | 客户端 shim：Anthropic Messages 或 OpenAI Responses 双协议 |
| `src/agent_harness/host.py` | agent 主循环：内置工具、团队、cron、MCP、memory、后台、压缩 |
| `src/agent_harness/memory_runtime.py` | memory 运行时：相关记忆选取与抽取 |
| `src/agent_harness/workflow.py` | 工作流运行时：journal、预算、resume、并行 agent |
| `src/agent_harness/goal.py` | 目标层：GoalController / PromptGoalEvaluator |
| `src/agent_harness/skills/` | 技能资产（随包分发） |
| `scripts/build_flat.py` | 拼接生成 `flat/agent_full.py` |
| `scripts/debug_pass.py` | 离线端到端自检，9 项，见 `note/dev-notes.md` |
| `flat/agent_full.py` | 生成物：全部机制在一个文件里 |
| `tests/` | 56 个离线测试 |
| `docs/` | 项目级设计与执行记录，入口见 `docs/README.md` |
| `note/` | `env-setup.md` 安装说明；`dev-notes.md` 本机运行与调试笔记 |
| `tmp/` | 本地临时文件，不入库（需要时手动 `mkdir tmp`） |
| `.vscode/` | VS Code 调试配置（launch/settings/extensions） |
| `AGENTS.md` | 本仓库的 agent 约定：环境、命令、编码、不变量 |
| `GLOSSARY.md` | 项目术语表，另含范围、现状与布局 |
| `pyproject.toml` | 元数据、依赖、console 入口、pytest 配置 |

## 行为要点

- **资源归属**：运行时状态（`.tasks` / `.memory` / `.runtime` / `.mailboxes` / `.worktrees` /
  `.scheduled_tasks.json` / `.transcripts` / `.task_outputs`）跟随**启动目录**；`skills/` 随包分发。
- **goal 接在 Stop 边界**：模型不再请求工具时先问目标控制器，返回 `block` 就补一条
  `[Goal still active]` 消息，继续同一条循环。
- **goal 状态只在内存**：重启后需要重新 `/goal`。
- **跨平台**：`fcntl` 用 try/except 包裹，无 fcntl 时退化为进程内锁并打印一次提示。
- **`.env` 与启动目录无关**：`load_dotenv()` 从包文件位置逐级向上查找，因此在任何目录
  启动都能读到项目根的 `.env`。

## 测试

```sh
python -m pytest -q
```

全部离线，不消耗 API：

- `tests/test_goal.py`：控制器用例，以及用脚本化 client 驱动 `agent_loop` 的会话级用例。
- `tests/test_workflow.py`：17 个用例，断言真实的工具池与 runner 接线。
- `tests/test_agent_teams.py`：合并宿主的运行时用例。
- `tests/test_flat_build.py`：拼接输出确定、与生成物一致、可编译、可运行。

离线端到端自检（覆盖 host 循环、工具防护、goal 边界、worktree、单文件版），说明见
[note/dev-notes.md](note/dev-notes.md)：

```sh
python scripts/debug_pass.py
```

断点调试：用 VS Code 的 Remote-WSL 窗口打开本目录，按 F5 选「Agent 交互（真实 API）」。
步骤见 [note/dev-notes.md](note/dev-notes.md)。

## 重新生成单文件版

改完 `src/agent_harness/` 下任一模块后：

```sh
python scripts/build_flat.py
```

`tests/test_flat_build.py::test_committed_flat_file_is_current` 会在生成物过期时报错。
