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

# 离线演示工作流：fake client，不联网
agent-harness workflow demo
agent-harness workflow resume
```

未安装成包时改用 `python -m agent_harness`，并先把源码目录放进 `PYTHONPATH`：

```sh
export PYTHONPATH=src        # Windows PowerShell: $env:PYTHONPATH="src"
python -m agent_harness
python -m agent_harness workflow demo
python -m agent_harness workflow resume
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/agent_harness/cli.py` | 入口：交互循环 + `/goal` 本地处理 + workflow demo |
| `src/agent_harness/__main__.py` | `python -m agent_harness` 入口 |
| `src/agent_harness/llm_client.py` | 客户端 shim：Anthropic Messages 或 OpenAI Responses 双协议 |
| `src/agent_harness/host.py` | 集成宿主：agent 主循环 + 上下文刷新 + 把各模块接线起来；状态路径由各模块按 `core.WORKDIR` 实时解析，换 cwd 无需重新绑定 |
| `src/agent_harness/core.py` | 共享配置：启动目录、API client、模型 id、常量、ConsoleBroker |
| `src/agent_harness/tasks.py` | 任务板：文件持久化的任务、依赖、认领/完成、assignment 注册表 |
| `src/agent_harness/worktrees.py` | Git worktree 的路径、注册表校验和 Git 调用 |
| `src/agent_harness/task_worktrees.py` | 任务与 worktree 绑定、移除的跨模块操作；移除时保留分支 |
| `src/agent_harness/skills.py` | 技能加载：扫描包内 `SKILL.md`，按名取用 |
| `src/agent_harness/prompts.py` | 系统提示词拼装：身份、工具目录、工作目录、技能、memory、MCP |
| `src/agent_harness/file_tools.py` | 基础工具：bash / 读写改文件 / glob / todo，含路径逃逸防护 |
| `src/agent_harness/hooks.py` | Hooks 与权限检查：UserPromptSubmit / PreToolUse / PostToolUse / Stop |
| `src/agent_harness/subagent.py` | 子 agent：统一创建、once/persistent 生命周期、信箱与计划/关闭协议 |
| `src/agent_harness/compaction.py` | 分层上下文压缩：工具结果瘦身、消息裁剪、摘要 |
| `src/agent_harness/recovery.py` | 重试与错误恢复：模型回退、重试预算 |
| `src/agent_harness/scheduler.py` | 后台任务与 cron 调度：`.scheduled_tasks.json` 持久化 |
| `src/agent_harness/mcp_tools.py` | MCP：连接后把 server 工具并入工具池 |
| `src/agent_harness/registrations.py` | 工具 schema 与 handler 表：工具名到实现的唯一映射 |
| `src/agent_harness/memory_runtime.py` | memory 运行时：相关记忆选取与抽取 |
| `src/agent_harness/runtime_context.py` | 每次模型调用前构造只读上下文快照 |
| `src/agent_harness/tool_registry.py` | 统一装配工具 schema、handler 和权限快照 |
| `src/agent_harness/message_content.py` | 共享文本提取和工具请求判断；不依赖生命周期 |
| `src/agent_harness/subagent_session.py` | 两种模式共用模型回合、工具执行、目录绑定和历史压缩 |
| `src/agent_harness/workflow_runtime.py` | 工作流运行时：journal、预算、resume、并行 agent |
| `src/agent_harness/workflow_demo.py` | 保存的示例工作流、schema 与离线 DemoModelClient；由 host/CLI 显式接线 |
| `src/agent_harness/goal.py` | 目标层：GoalController / PromptGoalEvaluator |
| `src/agent_harness/skills/` | 技能资产（随包分发） |
| `scripts/debug_pass.py` | 离线端到端自检，9 项，见 `note/dev-notes.md` |
| `tests/` | 离线测试，按模块拆分（协议、任务、压缩、调度、权限、编码、技能等） |
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
- **子 agent**：统一用 `spawn_subagent(name, prompt, mode)` 创建；模式、通信和后续任务由模型决定。
  能力与生命周期见 [调试笔记第 9 节](note/dev-notes.md#9-统一子-agent-生命周期与协议)。
- **工作流步骤**：`ctx.agent` 通过同一入口执行 once 子 agent，默认开放五项基础工具；
  `tools=[]` 只处理文本，名单可限制工具。预算、格式重试与缓存恢复仍由工作流管理，见 [调试笔记第 12 节](note/dev-notes.md#12-工作流单次-agent-调用)。
- **goal 接在 Stop 边界**：模型不再请求工具时先问目标控制器，返回 `block` 就补一条
  `[Goal still active]` 消息，继续同一条循环。
- **goal 状态只在内存**：重启后需要重新 `/goal`。
- **响应有效性**：Responses 失败状态和非法、截断的工具调用会交付明确错误；不执行部分工具。
- **历史压缩**：失败保留原历史，自动或手动失败后继续主请求；真正超限时只尝试一次被动恢复。
- **后台结果**：超过 200 字符的通知保留全文路径，全文随启动目录保存；绑定 worktree 的任务仍可通过 `read_file` 读取运行时归档，保存失败后整批等待重试。
- **Lead 恢复**：每回合独立管理输出升级、续写和一次被动压缩；429 会打断连续 529 的计数。
- **worktree 清理**：Lead 可请求 `remove_worktree`，移除目录并保留分支，不自动合并。
  完成、占用检查和分配释放时机见 [调试笔记第 8 节](note/dev-notes.md#8-任务与-worktree-绑定)。
- **workflow resume**：相同调用键回放 journal，不再次调用模型；预算包含该 run 的历史 token，
  返回的 `usage` 是本次新增用量，`cumulativeUsage` 是整个 run 的累计用量。旧格式 journal
  缺少 `tokens` 时会报错；重新运行 `workflow demo` 可生成新格式记录。
- **定时任务**：持久化的一次性任务在模型接受后确认，确认前中断可能重投；循环 cron 仅在
  当前进程内按分钟去重，重启后同一分钟可能再次触发。
- **跨平台**：`fcntl` 用 try/except 包裹，无 fcntl 时退化为进程内锁并打印一次提示。
- **`.env` 与启动目录无关**：`load_dotenv()` 从包文件位置逐级向上查找，因此在任何目录
  启动都能读到项目根的 `.env`。

## 测试

```sh
python -m pytest -q
```

全部离线，不消耗 API：

- `tests/test_goal.py`：控制器用例，以及用脚本化 client 驱动 `agent_loop` 的会话级用例。
- `tests/test_workflow.py`：覆盖 journal 回放、预算与 runner 接线。
- `tests/test_workflow_subagents.py`：实际 Workflow 工具、子 agent 文件操作、权限、格式重试和失败消费的恢复。
- `tests/test_llm_client.py`：覆盖响应协议、工具参数和失败响应的 Lead 行为。
- `tests/test_subagents.py`：宿主与任务/审批协议用例。
- `tests/test_subagent_lifetimes.py`：统一创建工具、真实线程、跨任务上下文和消息投递。
- `tests/test_shared_session.py`：共享执行、截断与上下文恢复。
- `tests/test_subagent_options.py`：工具白名单、依赖注入及模型/摘要用量计数。
- `tests/test_worktree_cleanup.py`：Lead 同轮清理、目录切换，以及拒绝和失败后的分配保留。

离线端到端自检（覆盖 host 循环、工具防护、goal 边界、worktree、子 agent），说明见
[note/dev-notes.md](note/dev-notes.md)：

```sh
python scripts/debug_pass.py
```

断点调试：用 VS Code 的 Remote-WSL 窗口打开本目录，按 F5 选「Agent 交互（真实 API）」。
步骤见 [note/dev-notes.md](note/dev-notes.md)。
