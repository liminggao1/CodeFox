# CodeFox 深度解析

## 一、项目概览

**CodeFox** 是一个本地运行的代码 Agent 环境。它把模型、工具、会话、任务、工作流和前端交互组织成一条完整的执行链，让 Agent 能够阅读代码、修改文件、运行命令、管理任务，并在需要时委派子 agent。

项目的重点不是单独封装一个模型客户端，而是维护一套可恢复的 Agent 运行时：

- CLI 和 Textual TUI 共用同一套交互运行时。
- Lead、一次性子 agent 和持久子 agent 共用同一个模型—工具循环。
- 会话、任务、工作流和工具结果分别按职责保存，支持停止、恢复和继续执行。
- 工具权限、审批、模型协议错误和执行失败都有明确的状态通道。

源码从 `src/codefox/` 开始，运行时状态跟随启动目录，会话上下文和诊断文件保存到 `~/.agent-harness/sessions/`。

---

## 二、整体架构

```text
┌────────────────────────────────────────────────────────────┐
│                         CodeFox                            │
│                                                            │
│  CLI / Textual TUI                                         │
│          │                                                 │
│          ▼                                                 │
│  InteractiveRuntime                                        │
│   ├─ CommandRouter / SessionSwitch                         │
│   ├─ TurnRunner                                             │
│   ├─ Scheduler / Hooks / Permissions                        │
│   └─ SessionStore / Interaction                            │
│          │                                                 │
│          ▼                                                 │
│  host.agent_loop / initialize_runtime                      │
│          │                                                 │
│          ▼                                                 │
│  LeadTurn / SubagentSession                                │
│          │                                                 │
│          ▼                                                 │
│  TurnEngine                                                 │
│   ├─ PromptState / Compaction                               │
│   ├─ Provider                                               │
│   ├─ ToolRegistry / ToolExecution                           │
│   └─ Goal / Recovery                                        │
│                                                            │
│  Workspace: tasks / assignments / Git worktrees             │
│  Workflow: journal / budget / resume                        │
└────────────────────────────────────────────────────────────┘
```

主要调用关系是：

```text
__main__.py
    └─ frontends.cli.main()
        ├─ CLIAdapter ───────────────┐
        ├─ Textual TUI ──────────────┤
        └─ workflow 路由              │
                                      ▼
                             InteractiveRuntime
                                      ▼
                                  TurnRunner
                                      ▼
                               host.agent_loop
                                      ▼
                                  TurnEngine
```

前端只负责输入和呈现，运行时负责会话与回合，Agent 层负责模型—工具循环，工具和工作区模块负责具体能力。

---

## 三、启动入口与运行状态

### 3.1 启动入口

`python -m codefox` 进入 `src/codefox/__main__.py`，再调用 `frontends.cli.main()`。安装项目后也可以直接执行 `codefox`。

CLI 的主要路由包括：

- 普通命令行交互
- `codefox tui` Textual 工作台
- `codefox workflow demo`
- `codefox workflow resume`

### 3.2 `core.py` 的职责

`core.py` 是运行时共享配置的来源，主要持有：

- 启动目录 `WORKDIR`
- 包资源目录 `PACKAGE_DIR` 和技能目录 `SKILLS_DIR`
- 模型客户端、主模型和备用模型
- 上下文窗口、压缩阈值和工具结果预算
- 重试、恢复、子 agent、调度器和 Shell 执行参数
- CLI 控制台读写协调器

运行时状态按启动目录保存，例如：

```text
.tasks/
.memory/
.runtime/
.mailboxes/
.worktrees/
.task_outputs/
.scheduled_tasks.json
```

会话上下文、诊断日志、压缩归档和长工具结果位于：

```text
~/.agent-harness/sessions/
```

这个边界很重要：启动目录决定项目运行状态，会话目录决定跨目录恢复所需的会话身份和归档。

---

## 四、一次用户回合的完整流程

用户输入后，CLI 或 TUI 把文本交给 `InteractiveRuntime`。运行时先通过 `TurnRunner.admit()` 判断当前是否可以接受提交，再决定它是本地命令、普通用户回合还是后台回合。

一次普通回合大致如下：

```text
用户输入
  │
  ▼
TurnRunner.admit()
  │
  ├─ 本地命令 ──► CommandRouter.dispatch()
  │                    └─ 可能产生 follow-up 回合
  │
  └─ 普通文本 ─► 记录 user_message
                       │
                       ▼
                 host.agent_loop()
                       │
                       ▼
                    TurnEngine
                       │
                       ├─ 准备上下文和工具快照
                       ├─ 请求模型
                       ├─ 处理文本或工具调用
                       ├─ 执行工具并收集结果
                       └─ 继续请求或结束回合
                       │
                       ▼
                 保存检查点和终态
                       │
                       ▼
                 发布 turn_finished
```

`TurnRunner` 持有提交排队、忙碌状态、停止事件、后台暂停标志和 Lead 用量账本。`InteractiveRuntime` 负责线程生命周期、启动和关闭，不把回合状态分散到前端。

---

## 五、TurnEngine：核心模型—工具循环

`agents/turn_engine.py` 是项目最核心的执行器。Lead、once 子 agent 和 persistent 子 agent 都通过它完成模型调用和工具执行。

### 5.1 阶段状态机

```text
CREATED
   │
   ▼
PREPARING ───────────────► WAITING_APPROVAL
   │                              │
   ▼                              ▼
WAITING_MODEL ───────► PROCESSING_RESPONSE
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
          EXECUTING_TOOLS                 COMPLETED
                 │
                 ├─ 工具需要审批 ─► WAITING_APPROVAL
                 └─ 工具完成 ─────► PREPARING

任何阶段都可能进入 STOPPED 或 FAILED。
```

引擎使用 `TurnCallbacks` 注入具体策略，包括：

- 如何准备 system、history 和 tools
- 如何请求模型
- 如何执行单个工具
- 每轮开始前如何更新上下文
- 回合完成前是否需要 goal、cron 或其他收尾判断

这样可以让执行顺序集中在 `TurnEngine`，而把 Lead 的恢复策略和子 agent 的预算策略留在各自的拥有者中。

### 5.2 请求快照

每次模型请求前，TurnEngine 生成 `PreparedRequest`，其中包含当前 system、历史和工具定义。重试使用同一请求快照，不重复追加通知或重新读取动态状态；当压缩或工具结果改变了历史时，下一轮再重新准备。

### 5.3 工具批次配对

模型返回多个工具调用时，执行器按调用 ID 生成对应的工具结果。普通工具失败不会破坏同批其他调用；致命 Hook 错误会先保存已经产生的真实结果，再补齐剩余调用的 stopped 结果，然后结束当前执行。

工具结果使用 `ToolResult` 表达状态，状态包括 `success`、`failed`、`denied`、`stopped` 和 `started`，调用方不需要通过正文猜测成功与否。

---

## 六、上下文、压缩与模型协议

### 6.1 PromptState

`context/prompt_state.py` 为 Lead 和子 agent 提供稳定的 system 快照。身份、规则、启动目录和技能目录在同一周期复用；记忆、MCP、子 agent、任务和工作目录等动态状态通过 `_runtime_context` 标记的运行通知追加到历史末尾。

动态通知只服务模型上下文，不生成页面消息，也不计入展示消息数量。会话恢复时复用保存的 prompt state；状态缺失时在下一次请求初始化，结构错误则明确报错并保留原文件。

### 6.2 分层压缩

`context/compaction.py` 的 `prepare_history()` 按层处理上下文：

1. 瘦身过大的工具结果。
2. 裁剪过长历史。
3. 调用模型生成摘要。
4. 需要时执行手动、自动或被动压缩。
5. 压缩失败时保留原历史。

当前默认值由 `core.py` 计算：

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `MODEL_CONTEXT_TOKENS` | `258000` | 模型和服务端共同支持的上下文窗口 |
| `ESCALATED_MAX_TOKENS` | `64000` | 长输出请求的输出预算 |
| 安全余量 | `10%` | 估算误差和请求开销 |
| 自动压缩触发 | 可用预算的 `90%` | 开始压缩的阈值 |
| 压缩目标 | 可用预算的 `80%` | 压缩后的目标大小 |
| 最新工具结果预算 | 可用预算的 `16%` | 最新工具批次占用上限 |

单条工具结果默认超过 `30000` 字符时落盘，历史中保留默认 `1000` 字符预览；原文路径会提供给模型或用户继续读取。

### 6.3 Provider 协议适配

`providers/llm_client.py` 将 Anthropic Messages 和 OpenAI Responses 两种协议转换成项目内部统一结构。通过 `LLM_API=responses` 切换 Responses 模式，调用方仍使用同一套消息和工具接口。

协议层严格校验响应：顶层不是对象、工具参数不是对象、工具调用身份缺失、流没有结束帧或工具批次被截断，都会抛出 `ProtocolResponseError`。这类响应整批不执行，也不自动补全 JSON 或重试。

---

## 七、工具、权限与审批

### 7.1 工具注册

工具由 `tools/tool_registry.py` 统一管理 schema、handler 和权限快照。共用工具定义位于 `shared_tools.py`，Lead 的具体清单由根目录 `registrations.py` 装配。

常用工具包括：

- `read_file`：读取文件
- `write_file`：创建或完整覆盖文件
- `edit_file`：定点修改已有文件
- `glob`：查找工作区内路径
- `bash`：搜索、测试、构建、Git 和其他 Shell 操作
- `todo_write`：替换 Lead 的 Todo 列表
- 任务和工作树工具
- MCP 动态工具

工具选择由提示词和工具描述引导。工具层不运行主循环，也不保存展示记录；执行状态经过运行时统一结算。

### 7.2 权限模式

权限由 `runtime/permissions.py` 集中判断，`hooks.py` 接入审批通道。默认有两种模式：

| 模式 | 行为 |
| --- | --- |
| `default` | 工作区内文件操作通常直接允许，目录外操作和复杂 Shell 需要审批 |
| `no_approval` | 原本需要询问的操作直接执行，但仍保留参数校验和禁止命令规则 |

有限的 `pwd`、`ls`、`cat` 和 `git status` 等简单命令可以免审批。脚本、管道、重定向、命令连接、变量展开和命令替换需要审批；`sudo`、关机、重启、`mkfs`、危险的 `dd` 和递归强制删除根目录等命令会被拒绝。

CLI、TUI、子 agent 和后台任务共用同一套权限规则。一次回合开始后，执行上下文固定该回合使用的权限模式。

---

## 八、子 agent 生命周期

统一入口是：

```text
spawn_subagent(name, prompt, mode, description=...)
```

`mode` 必须明确为 `once` 或 `persistent`。

### 8.1 once

- 在工具调用内同步等待。
- 返回最终结果、状态和用量。
- 可绑定指定 Task。
- 完成后清理分配。
- 用户停止会结束当前 once 工作。

### 8.2 persistent

- 创建稳定 `agent_id` 后启动后台线程。
- 立即返回 `started`。
- 通过信箱接收 Lead 或其他持久子 agent 的消息。
- 空闲时等待下一项工作，不主动请求模型。
- 收到关闭请求、失败或预算耗尽后进入对应终态。

每个工作单元默认最多：

| 限制 | 默认值 |
| --- | ---: |
| 模型请求轮数 | `12` |
| 工具调用次数 | `40` |

限制可以在创建时通过 `max_rounds` 和 `max_tool_calls` 覆盖。预算耗尽时直接返回 `budget_exhausted` 和部分结果，不额外请求总结。

子 agent 使用与 Lead 相同的 TurnEngine，但工具白名单、cwd、预算、提示词状态和恢复策略由 `SubagentSession` 独立持有。子 agent 不再递归创建新的子 agent。

---

## 九、任务板、工作树与后台调度

### 9.1 任务板和 Git 工作树

`workspace/tasks.py` 负责任务持久化、依赖、认领、完成和 assignment；`worktrees.py` 只负责 Git 工作树和注册表；`task_worktrees.py` 协调任务与工作树绑定、占用检查和移除。

任务目录解析遵循当前 Lead 或子 agent 的有效分配。没有分配时使用启动目录，分配失效时明确报错。Lead 移除工作树时保留 Git 分支，不自动合并。

### 9.2 后台任务和 cron

`runtime/scheduler.py` 独占后台任务、cron 队列和调度锁。后台结果先保存长文本，再生成通知，最后交给 Lead 的后台回合消费。

循环 cron 在当前进程内按分钟去重；持久化的一次性任务采用至少一次交付：入队前落盘，模型调用被接受后才确认，确认前崩溃允许重新投递。

---

## 十、工作流运行时

工作流不是另一套 Agent 执行器，而是用程序组织多个 once 子 agent 步骤。

```text
WorkflowTool
    ├─ 校验 meta 和权限
    ├─ 创建或恢复 runId
    ├─ 打开 journal
    ├─ 执行 ctx.agent / parallel / pipeline / workflow
    ├─ 累计 token budget
    ├─ 保存 snapshot 和 output
    └─ 发布 task_notification
```

`workflow_runtime.py` 提供：

- schema 校验
- 并发步骤和流水线
- token budget
- 语义键 journal
- 失败、取消和恢复
- 一层嵌套子工作流

相同 `prompt/label/schema/tools` 的步骤共享稳定语义键。恢复命中 journal 时直接复用结果，不再次调用模型；预算包含整个 run 的历史消费。

离线演示由 `workflow_demo.py` 提供，执行：

```sh
codefox workflow demo
codefox workflow resume
```

---

## 十一、会话保存与恢复

每个交互会话对应一个权威文件：

```text
~/.agent-harness/sessions/<session_id>.json
```

会话保存的核心内容包括：

- 模型历史
- 展示记录
- Todo 和会话状态
- prompt state 快照
- 当前工作树和任务归属信息
- 用量摘要

`/resume` 无参数时列出历史会话，带 ID 时读取并校验指定文件。恢复使用当前结构；文件缺失字段、目录移动或结构不符时明确报错，不转换旧格式，也不覆盖原文件。

保存采用临时文件加原子替换。运行时在关键事件、审批创建和工具结算处保存检查点，使停止或进程退出后仍能恢复已经接受的事实。

---

## 十二、关键文件速查

| 文件 | 作用 |
| --- | --- |
| `src/codefox/__main__.py` | `python -m codefox` 入口 |
| `src/codefox/core.py` | 启动目录、配置、模型客户端和运行常量 |
| `src/codefox/host.py` | Lead 装配、`agent_loop` 和初始化入口 |
| `src/codefox/agents/turn_engine.py` | 共用模型—工具循环和阶段状态机 |
| `src/codefox/agents/lead_turn.py` | Lead 的恢复、goal、cron 和 memory 收尾 |
| `src/codefox/agents/subagent.py` | once/persistent 子 agent 生命周期 |
| `src/codefox/agents/subagent_session.py` | 子 agent 工具、预算和回合装配 |
| `src/codefox/runtime/interactive_runtime.py` | CLI/TUI 共用交互生命周期 |
| `src/codefox/runtime/turn_runner.py` | 提交排队、停止和回合终态 |
| `src/codefox/runtime/session_store.py` | 会话保存、读取和恢复准备 |
| `src/codefox/runtime/permissions.py` | 权限模式和规则判断 |
| `src/codefox/context/prompt_state.py` | system 快照和动态运行通知 |
| `src/codefox/context/compaction.py` | 历史压缩和工具结果落盘 |
| `src/codefox/providers/llm_client.py` | Anthropic/Responses 协议适配 |
| `src/codefox/tools/tool_registry.py` | 工具 schema、handler 和策略快照 |
| `src/codefox/workspace/tasks.py` | 任务板和 assignment |
| `src/codefox/workspace/worktrees.py` | Git 工作树 |
| `src/codefox/workflows/workflow_runtime.py` | 工作流预算、journal 和恢复 |

---

## 十三、运行与验证

项目默认在 WSL2 的 `learncc` 环境中运行，从源码启动：

```sh
PYTHONPATH=src python -m codefox
PYTHONPATH=src python -m codefox tui
```

开发验证：

```sh
python -m pytest -q
python scripts/debug_pass.py
```

真实模型测试单独启用：

```sh
LIVE_API=1 PYTHONPATH=src python -m pytest -q tests/live
```

配置模型至少需要在 `.env` 中提供：

```dotenv
ANTHROPIC_API_KEY=your-api-key
MODEL_ID=your-model-id
```

长期记忆默认关闭，设置 `AGENT_MEMORY_ENABLED=1` 后启用；Responses 协议通过 `LLM_API=responses` 切换。

---

## 十四、总结：这个项目的核心设计

1. **执行循环集中**：Lead 和两种子 agent 共享 TurnEngine，减少多套循环之间的行为漂移。
2. **状态职责清楚**：运行时、会话、任务、工作树、工具、上下文和工作流分别拥有自己的状态。
3. **事实先保存再展示**：事件、工具结果和诊断日志有独立通道，停止或恢复时不会只依赖屏幕内容。
4. **协议错误不猜测修复**：非法模型响应直接失败，避免把不完整工具调用执行出去。
5. **上下文有预算意识**：PromptState、压缩、工具结果落盘和记忆召回共同控制输入规模。
6. **子 agent 生命周期明确**：once 用于同步委派，persistent 用于持续协作，消息和预算边界清晰。
7. **工作流可恢复**：journal 让已完成步骤可以复用，失败和取消不会丢失累计用量。

理解这个项目时，建议先按 `frontends → runtime → host → agents/turn_engine.py` 阅读主执行链，再分别进入 `context`、`tools`、`workspace` 和 `workflows`。
