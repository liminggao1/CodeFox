# agent-harness 术语表与项目上下文

## Glossary

- **host**：`host.py`，把模型能做的事集中在一起；入口在 `cli.py`。
- **workflow runtime**：`workflow.py`，可 resume 的多 agent 工作流；每次 `agent()` 调用进 journal。
- **goal**：`goal.py`，接在 agent 循环的 Stop 边界；模型不再请求工具时决定放行还是继续。
- **runner**：工作流里执行单次 agent 调用的执行器；`AnthropicAgentRunner` 走真实 API，`MockAgentRunner` 离线。
- **journal**：workflow 每次调用的输入/输出记录，resume 时命中缓存；损坏或参数不符会报错。
- **flat build**：`scripts/build_flat.py` 把四个模块拼成单文件 `flat/agent_full.py`，供独立阅读运行。
- **WORKDIR**：运行时状态目录的基准，取启动时的 `Path.cwd()`；与随包的 `skills/` 相对。

## Scope

一个本地 code agent 运行环境：提供 agent 主循环、内置工具、团队协作、工作流运行时与目标
控制，打包成可安装、可继续开发的 Python 包。目标是可长期演进的本地 agent harness，
保存完整机制，不做裁剪。

## Current State

- 结构：`src/agent_harness/`（源码 + 包内 `skills/`）、`scripts/`、`flat/`、`tests/`、`docs/`、`note/`。
- 运行：本地以源码方式运行，环境约定见 `AGENTS.md`；通用安装步骤见 `note/env-setup.md`。
- 验证：108 个离线测试全绿；workflow `demo` / `resume` 离线通过；离线自检 9 项通过。
- 未实测：真实 API 交互问答、队友线程端到端、memory 抽取。
- 不做打包发布，不引入 linter/formatter。

## Layout

| 路径 | 内容 |
| --- | --- |
| `src/agent_harness/host.py` | 集成宿主：agent 主循环、上下文刷新、模块接线 |
| `src/agent_harness/core.py` | 共享配置：启动目录、client、模型 id、常量 |
| `src/agent_harness/tasks.py` | 任务板：任务、依赖、认领/完成、assignment 注册表 |
| `src/agent_harness/worktrees.py` | 任务绑定的 Git worktree |
| `src/agent_harness/skills.py` | 技能加载 |
| `src/agent_harness/prompts.py` | 系统提示词拼装 |
| `src/agent_harness/file_tools.py` | 基础工具：bash / 文件 / glob / todo |
| `src/agent_harness/teams.py` | 持久团队：mailbox、协议、队友线程 |
| `src/agent_harness/hooks.py` | Hooks 与权限检查 |
| `src/agent_harness/subagent.py` | 子 agent 运行 |
| `src/agent_harness/compaction.py` | 分层上下文压缩 |
| `src/agent_harness/recovery.py` | 重试与错误恢复 |
| `src/agent_harness/scheduler.py` | 后台任务与 cron 调度 |
| `src/agent_harness/mcp_tools.py` | MCP 工具接入 |
| `src/agent_harness/registrations.py` | 工具 schema 与 handler 表 |
| `src/agent_harness/workflow.py` | 工作流运行时：定义、journal 缓存、预算、resume、并行 agent |
| `src/agent_harness/goal.py` | 目标层：GoalController + PromptGoalEvaluator |
| `src/agent_harness/memory_runtime.py` | memory 运行时：相关记忆选取与抽取 |
| `src/agent_harness/llm_client.py` | 客户端 shim：Anthropic Messages / OpenAI Responses 双协议 |
| `src/agent_harness/cli.py` | 入口：交互 + `/goal` + workflow demo |
| `scripts/build_flat.py` | 拼接生成单文件版 |
