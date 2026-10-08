# agent-harness 术语表与项目上下文

## Glossary

- **host**：`host.py`，把模型能做的事集中在一起；入口在 `cli.py`。
- **workflow runtime**：`workflow.py`，可 resume 的多 agent 工作流；每次 `agent()` 调用进 journal。
- **goal**：`goal.py`，接在 agent 循环的 Stop 边界；模型不再请求工具时决定放行还是继续。
- **runner**：工作流里执行单次 agent 调用的执行器；`AnthropicAgentRunner` 走真实 API，`MockAgentRunner` 离线。
- **journal**：workflow 按调用键记录结果与 token 用量；resume 命中缓存时不重复计费。
- **flat build**：`scripts/build_flat.py` 拼接源码模块成 `flat/agent_full.py`，作为仓库内的单文件入口。
- **WORKDIR**：运行时状态目录的基准，取启动时的 `Path.cwd()`；与随包的 `skills/` 相对。

## Scope

一个本地 code agent 运行环境：提供 agent 主循环、内置工具、团队协作、工作流运行时与目标
控制，打包成可安装、可继续开发的 Python 包。目标是可长期演进的本地 agent harness，
保存完整机制，不做裁剪。

## Current State

- 结构：`src/agent_harness/`（源码 + 包内 `skills/`）、`scripts/`、`flat/`、`tests/`、`docs/`、`note/`。
- 运行：本地以源码方式运行，环境约定见 `AGENTS.md`；通用安装步骤见 `note/env-setup.md`。
- 验证：124 个离线测试；workflow `demo` / `resume` 离线运行；离线自检 9 项。
- 未实测：真实 API 交互问答、队友线程端到端、memory 抽取。
- 不做打包发布，不引入 linter/formatter。

## Layout

| 路径 | 内容 |
| --- | --- |
| `src/agent_harness/host.py` | 集成宿主：agent 主循环、上下文刷新、模块接线 |
| `src/agent_harness/core.py` | 共享配置：启动目录、client、模型 id、常量 |
| `src/agent_harness/tasks.py` | 任务板：任务、依赖、认领/完成、assignment 注册表 |
| `src/agent_harness/worktrees.py` | Git worktree 路径、注册表和 Git 调用 |
| `src/agent_harness/task_worktrees.py` | 任务与 Git worktree 的绑定和移除 |
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
| `src/agent_harness/runtime_context.py` | RuntimeContext：每次模型调用前构造的只读运行时快照 |
| `src/agent_harness/tool_registry.py` | ToolRegistry：内置、MCP 和 Workflow 工具的装配 seam |
| `src/agent_harness/teammate_session.py` | TeammateSession：不拥有 mailbox 或线程生命周期的执行 adapter |
| `src/agent_harness/llm_client.py` | 客户端 shim：Anthropic Messages / OpenAI Responses 双协议 |
| `src/agent_harness/cli.py` | 入口：交互 + `/goal` + workflow demo |
| `scripts/build_flat.py` | 拼接生成单文件版 |
