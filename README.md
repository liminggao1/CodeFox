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
agent-harness tui            # Textual 界面，需安装 tui 可选依赖

# 离线演示工作流：fake client，不联网
agent-harness workflow demo
agent-harness workflow resume
```

未安装成包时改用 `python -m agent_harness`，并先把源码目录放进 `PYTHONPATH`：

```sh
export PYTHONPATH=src        # Windows PowerShell: $env:PYTHONPATH="src"
python -m agent_harness
python -m agent_harness tui
python -m agent_harness workflow demo
python -m agent_harness workflow resume
```

Textual 的本机安装、启动、快捷键、审批和会话操作见
[开发笔记：Textual 交互界面](note/dev-notes.md#textual-交互界面)。包安装方式可使用
`pip install -e ".[dev,tui]"`；默认 CLI 和离线 workflow 不加载 Textual。

## 目录

源码的文件分工、调用关系和职责边界见 [源码结构说明](src/agent_harness/README.md)。

| 路径 | 内容 |
| --- | --- |
| [src/agent_harness/](src/agent_harness/README.md) | 包入口、共享配置、Lead 主循环和装配代码 |
| [frontends/](src/agent_harness/frontends/README.md) | CLI / Textual 输入与显示 |
| [runtime/](src/agent_harness/runtime/README.md) | 共享交互驱动、命令、事件/审批、会话存储与调度 |
| [agents/](src/agent_harness/agents/README.md) | 子 agent 执行、goal 与恢复控制 |
| [context/](src/agent_harness/context/README.md) | 提示词、历史压缩、memory 与技能加载 |
| [providers/](src/agent_harness/providers/README.md) | 模型协议适配和消息内容读取 |
| [tools/](src/agent_harness/tools/README.md) | 基础工具、MCP 与工具注册机制 |
| [workspace/](src/agent_harness/workspace/README.md) | 任务板、分配与 Git 工作树 |
| [workflows/](src/agent_harness/workflows/README.md) | 程序化工作流、预算与 journal 恢复 |
| `src/agent_harness/skills/` | 技能资产（随包分发） |
| `assets/logo/` | Logo 素材、可编辑 SVG 与导出的图片 |
| `scripts/debug_pass.py` | 离线端到端自检，9 项，见 `note/dev-notes.md` |
| `tests/` | 离线测试，按模块拆分（协议、任务、压缩、调度、权限、编码、技能等） |
| `docs/` | 项目级设计与执行记录，入口见 `docs/README.md` |
| `note/` | `env-setup.md` 安装说明；`dev-notes.md` 本机运行与调试笔记 |
| `tmp/` | 本地临时文件，不入库（需要时手动 `mkdir tmp`） |
| `.vscode/` | VS Code 调试配置（launch/settings/extensions） |
| `AGENTS.md` | 本仓库的 agent 约定：环境、命令、编码、不变量 |
| `GLOSSARY.md` | 项目术语表：会话、上下文、任务、工作目录等概念的统一名称 |
| `pyproject.toml` | 元数据、依赖、console 入口、pytest 配置 |

直接导入实现时使用新子包路径，例如
`from agent_harness.runtime.interactive_runtime import InteractiveRuntime`。

## 行为要点

- **共同工具执行**：Lead 和子 agent 共用前置检查与同步执行；授权或 hook 只有返回 None 才放行。
  Lead 停止优先于前置拒绝；compact 保留特殊路径，后台的后置 hook 在实际完成后执行。

- **长期记忆**：`AGENT_MEMORY_ENABLED=1` 才启用记忆召回、回合结束提取和记忆整理；默认关闭，
  避免每轮最终回答后再次请求模型。历史压缩是独立的上下文安全机制，不受此开关控制。

- **资源归属**：运行时状态（`.tasks` / `.memory` / `.runtime` / `.mailboxes` / `.worktrees` /
  `.scheduled_tasks.json` / `.transcripts` / `.task_outputs`）跟随**当前会话的启动目录**；会话上下文统一存放在用户目录 `~/.agent-harness/sessions/`；`skills/` 随包分发。
- **子 agent**：统一用 `spawn_subagent(name, prompt, mode)` 创建；模式、通信和后续任务由模型决定。
  能力与生命周期见 [调试笔记第 9 节](note/dev-notes.md#9-统一子-agent-生命周期与协议)。
- **工作流步骤**：`ctx.agent` 通过同一入口执行 once 子 agent，默认开放五项基础工具；
  `tools=[]` 只处理文本，名单可限制工具。预算、格式重试与缓存恢复仍由工作流管理，见 [调试笔记第 12 节](note/dev-notes.md#12-工作流单次-agent-调用)。
- **goal 接在 Stop 边界**：模型不再请求工具时先问目标控制器，返回 `block` 就补一条
  `[Goal still active]` 消息，继续同一条循环。
- **交互会话**：第一次有效用户交互创建 `~/.agent-harness/sessions/<id>.json`，历史、goal、任务关联、工作目录和 TODO 变化都更新同一文件；`/resume` 列出会话，`/resume <id>` 恢复上下文，`/delete <id>` 删除非活动会话。CLI 与 TUI 共用后端命令与展示记录。
- **对话展示**：消息、工具调用与结果、TODO、后台进度、审批和回合摘要随会话保存；模型历史压缩不删除展示记录。恢复只读重放历史，不重执行工具或审批。尚无结果的工具提示核对当前状态；未结束的历史审批无操作按钮。TUI 详情默认收起，可加载更早记录；操作见 [开发笔记](note/dev-notes.md#textual-交互界面)。
- **会话切换**：可从任意目录启动，按 ID 恢复后自动回到原启动目录，工具继续使用原任务工作树。恢复前必须没有正在运行或待交付的后台任务、子 agent、Lead 信箱事件或定时任务结果；目标校验不改变当前目录，校验、保存当前上下文或切目录失败均不切换。目标激活后保存失败，继续使用目标上下文并提示下次交互重试保存。跨目录切换时停止旧目录的调度，加载原目录的持久化定时任务，旧目录的非持久化定时任务不保留。Lead 任务按会话归属隔离，切换和退出保留未完成任务；完成任务后会话继续使用原工作树，成功移除工作树后回到启动目录。原目录不存在、任务归属、绑定或工作树失效时拒绝恢复，旧线程和连接不恢复。
- **恢复后的异常**：目标已经激活后，信箱读取等收尾处理失败会报告错误并保留目标上下文；两个前端仍收到会话切换与命令结束通知。损坏的后台输入暂停消费，修复后可继续交互。
- **会话格式**：只维护一种 JSON 上下文格式，不保存版本号；读取时校验启动目录、任务 ID、工作树名称、TODO，以及消息与展示记录。展示记录在 `events` 中，模型历史仍在 `messages` 中。缺少必要字段的文件提示错误并保留；不转换旧文件，重新交互可生成当前结构。原启动目录的 `.transcripts/sessions/` 不再扫描；目录移动后的恢复不支持。
- **响应有效性**：Responses 失败状态和非法、截断的工具调用会交付明确错误；不执行部分工具。
- **历史压缩**：失败保留原历史，自动或手动失败后继续主请求；真正超限时只尝试一次被动恢复。
- **后台结果**：超过 200 字符的通知保留全文路径，全文随启动目录保存；绑定 worktree 的任务仍可通过 `read_file` 读取运行时归档，保存失败后整批等待重试。
- **Lead 恢复**：每回合独立管理输出升级、续写和一次被动压缩；429 会打断连续 529 的计数。
- **worktree 清理**：Lead 可请求 `remove_worktree`，移除目录并保留分支，不自动合并。
  完成、占用检查和分配释放时机见 [调试笔记第 8 节](note/dev-notes.md#8-任务与-worktree-绑定)。
- **workflow resume**：相同调用键回放 journal，不再次调用模型；预算包含该 run 的历史 token，
  返回的 `usage` 是本次新增用量，`cumulativeUsage` 是整个 run 的累计用量。旧格式 journal
  缺少 `tokens` 时会报错；重新运行 `workflow demo` 可生成新格式记录。
- **workflow 收尾**：普通失败等待已登记的步骤与编排组结算；取消阻止新调用，等待已启动调用，
  保存 failed 后重抛原取消。实际消费先计入预算；超额后禁止新调用，本次运行保持 failed。
  有效结果只在未超额、未取消时缓存；格式修复和失败调用的已知消费也计入累计用量。
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

共用设施放在 `tests/support/`：`fakes.py` 提供假模型响应构造器，`helpers.py` 提供运行时事件和会话文件工具；`conftest.py` 提供 host 与运行时工厂。

- `tests/test_goal.py`：控制器用例，以及用脚本化 client 驱动 `agent_loop` 的会话级用例。
- `tests/test_workflow.py`：覆盖 journal 回放、预算与 runner 接线。
- `tests/test_workflow_subagents.py`：实际 Workflow 工具、子 agent 文件操作、权限、格式重试和失败消费的恢复。
- `tests/test_llm_client.py`：覆盖响应协议、工具参数和失败响应的 Lead 行为。
- `tests/test_subagents.py`：宿主与任务/审批协议用例。
- `tests/test_subagent_lifetimes.py`：统一创建工具、真实线程、跨任务上下文和消息投递。
- `tests/test_shared_session.py`：共享执行、截断与上下文恢复。
- `tests/test_subagent_options.py`：工具白名单、依赖注入及模型/摘要用量计数。
- `tests/test_worktree_cleanup.py`：Lead 同轮清理、目录切换，以及拒绝和失败后的分配保留。
- `tests/test_interaction.py`：事件顺序与数据副本、审批关联、来源权限、停止和关闭解除等待。
- `tests/test_interactive_runtime.py`：实际共享驱动、命令/会话、四种回合结果、停止边界、cron 确认、后台交付和 CLI 读取归属。
- `tests/test_tui_integration.py`：无头界面的中文多行对话、审批、停止、恢复、窄屏和滚动；未安装 Textual 时跳过。
- `tests/test_conversation_records.py`：实际文件中的展示记录、审批前保存、后台检查点、重试去重、压缩与恢复续聊、当前结构校验。
- `tests/test_conversation_frontends.py`：CLI 历史与文本详情、TUI 内联审批、展开和分段加载；历史只读并按 ID 去重。

离线端到端自检（覆盖 host 循环、工具防护、goal 边界、worktree、子 agent），说明见
[note/dev-notes.md](note/dev-notes.md)：

```sh
python scripts/debug_pass.py
```

断点调试：用 VS Code 的 Remote-WSL 窗口打开本目录，按 F5 选「Agent 交互（真实 API）」。
步骤见 [note/dev-notes.md](note/dev-notes.md)。
