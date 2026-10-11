# 仓库事实

本文记录 agent-harness 当前生效的架构与行为事实，按仓库演进里程碑组织。
只描述当前状态，不记录规划过程、实施顺序与当时的测试数；历史决策由 Git 保留。

权威分工：运行方式与代码布局见 [README.md](../../README.md)，术语见 [GLOSSARY.md](../../GLOSSARY.md)，
文件分工见 [源码结构说明](../../src/agent_harness/README.md) 与各子包 README，本机环境见 [AGENTS.md](../../AGENTS.md)。
本文只维护跨模块的架构与行为规则；同一事实不在两处并列，本文与上述文件冲突时以代码为准。

## 1. 结构初始化（2026-10-04）

项目由 s15 集成宿主、s16 workflow 运行时与 s17 goal 循环合并而来，现为自有的本地 code agent
运行环境；不打包发布，不做多运行时实例隔离。

- 布局为 `src/agent_harness/`，console 入口 `agent-harness` 与 `python -m agent_harness`；包内用相对导入。
- 资产随包分发：`skills/` 位于包内，`SKILLS_DIR = PACKAGE_DIR / "skills"`，editable 与 wheel 安装都能解析。
- 运行时状态跟随**当前会话的启动目录**：`.tasks`、`.memory`、`.mailboxes`、`.worktrees`、`.runtime`、
  `.scheduled_tasks.json`、`.transcripts`、`.task_outputs`；会话上下文在 `~/.agent-harness/sessions/`。
- `.env` 由 `load_dotenv()` 从包文件位置逐级向上查找，与启动目录无关。
- memory 以正常导入接入并共享 client / model / workspace，不再按路径 `importlib` 加载。
- workflow 直接进入工具池；`run_workflow_sync` 用 `asyncio.run` 桥接同步宿主与异步运行时。
- goal 接在 Stop 边界：模型不再请求工具时先问 `GoalController`，`block` 则补 `[Goal still active]` 继续；
  goal 状态只存在内存，默认 `block_cap=8`、`MAX_GOAL_LENGTH=4000`。
- 跨平台：`fcntl` 用 try/except 包裹，无 fcntl 时退化为进程内锁并打印一次提示。
- 文档骨架：`docs/{adr,plans,research,handoff,guides,agents}/`、`note/`、`.scratch/`（本地议题）、`tmp/`（本地临时，不入库）。

## 2. 架构调整（2026-10-06）

目标是减少定位一次工具调用、任务分配和恢复流程时跨越的模块数，并明确可变状态的所有者。

- 任务归属三分：`workspace/tasks.py` 负责任务持久化、认领、assignment 与目录查询；
  `workspace/worktrees.py` 只负责 Git 与目录；`workspace/task_worktrees.py` 负责两者的绑定与移除
  （含占用检查、跨进程任务锁、部分成功后的报错）。
- 依赖方向为 `agents` / `tools` → `workspace.tasks` → `workspace.worktrees`；`worktrees` 不反向导入任务。
- 启动目录、配置、client 与 CLI 状态由 `core.py` 持有；任务、memory、workflow、transcript、工具结果
  与提示词在调用时读取当前目录，`host` 不再维护 client 副本。
- workflow journal 记录每次新调用及累计 token 用量；相同 `prompt/label/schema` 的并发调用只执行一次；
  缺 `tokens` 的旧格式记录在 resume 时明确报错（旧文件保留，重跑生成新格式）。
- cron：循环 cron 仅在本进程内按分钟去重，重启后同一分钟可再次触发；持久化的一次性任务采用至少一次交付
  （入队前落盘，模型调用被接受后才确认，确认前崩溃允许重投）。
- Lead 与子 agent 保持独立循环、独立工具集合与停止条件；只共用底层文件工具实现、handler 调用和 hook 规则。

## 3. 两轮架构深化（2026-10-08）

第一轮把浅 module 加深，第二轮修正协议与恢复行为。当前事实：

- `context/memory_runtime.py` 只保留记忆能力（存储、索引、检索、抽取、整理）；旧 Agent runtime 已删除。
- `runtime_context.py` 以 frozen dataclass 和 tuple 持有上下文值，`build_context(messages)` 是唯一构造入口；
  Lead 在每次模型调用前刷新，CLI 与后台循环不持有共享 context。
- `tools/tool_registry.py` 统一装配工具 schema、handler 与 policy 快照，来源由宿主显式接线；
  `ToolDefinition` 是内置工具的权威目录；MCP 只提供动态定义。
- `agents/subagent_session.py` 的 `SubagentSession` 拥有工具 schema、cwd 绑定、hooks 和一轮模型执行；
  目录在工具调用时查询当前 assignment，不保存会过期的任务目录。
- `workflows/workflow_runtime.py` 与 `workflows/workflow_demo.py` 分离：核心拥有 runner interface、journal、
  预算、恢复、并发与工具入口；示例拥有 schema、样例编排、`DemoModelClient` 与 demo CLI。
- host 聚合重导出已删除，仓库调用方、测试和调试脚本直接导入真实职责 module，不再经 host 别名。
- Provider 保证交付的响应可消费：非法 JSON、顶层非对象、缺失调用身份与截断的工具批次都不转成正常结果，
  统一抛 `ProtocolResponseError`，不自动补括号、字段或参数值。
- `context/compaction.py` 的 `prepare_history` 拥有完整分层策略（瘦身、裁剪、摘要、手动/自动压缩、被动恢复）；
  自动、手动与被动入口都用 deepcopy 候选历史和切片提交，返回只读 `CompactionResult`。
- 后台结果领取先保存所有超过 200 字符的全文到 `.task_outputs/tool-results/background/`，再生成通知，
  最后移除整批记录；长通知包含全文路径，已保存路径在重试时复用。
- `agents/recovery.py` 的 `RecoveryState` 提供只读模型/额度/计数和固定转换方法，集中 Lead 的重试与恢复状态；
  429 与成功调用都打断连续 529 计数。

## 4. 统一子 agent（2026-10-08）

一次性子代理与持久实例统一称为**子 agent**，由模型通过运行模式选择生命周期。

- 创建入口唯一：`spawn_subagent(name, prompt, mode, role)`；`mode` 必须显式为 `once` 或 `persistent`，
  运行时不猜测模式。没有工具别名。
- `once` 在工具调用内同步等待，返回 `completed` 或 `failed`；`persistent` 登记并启动线程后返回 `started`，
  结果与错误随后经信箱事件交付。模式在创建时固定，存活期间不变。
- 创建工具返回 JSON 文本，字段为 `name` / `mode` / `status` / `text` / `usage`；校验失败时只回填已验证值。
- `agents/subagent.py` 拥有创建、生命周期、活跃登记（`active_subagents`）、信箱与协议投递；
  `agents/subagent_session.py` 提供两种模式共用的模型回合。
- 管理工具：`list_subagents`、`send_message`、`request_shutdown`、`request_plan`、`review_plan`、
  `remove_worktree`（仅 Lead）。任何子 agent 都不提供 `spawn_subagent`、依赖修改或 worktree 创建/移除，
  保持不可递归。
- 通信由发送方模型决定；运行时只负责投递、唤醒和状态通知。执行结果、失败、IDLE 与关闭确认仍由运行时通知。
- 不使用任务板时用 `send_message` 委派下一次工作；使用任务板时 Lead 发任务 ID，子模型先 `claim_task(task_id)`
  再执行。子模型可调用 `list_tasks` 决定是否认领；每个实例同时最多占用一个任务。
- 已移除空闲线程定时扫描自动认领第一个任务的行为。
- 目录规则：有有效分配时用任务目录，无分配时用启动目录，分配失效时报错；任务状态与目录租约只由 tasks 管理。
- 工具能力：两种模式共用 bash、读、写、编辑、glob；`persistent` 追加 `send_message` / `submit_plan`，
  有任务时提供任务管理工具；`once` 完成后汇报并结束，`persistent` 汇报后进入 IDLE 并保留上下文。
- 工作流的 agent 步骤（`ctx.agent`）通过同一入口执行 once 子 agent，默认开放五项基础工具；`tools=[]` 只处理文本。
- 上下文压缩复用 `compaction`；实例身份与历史列表对象保留，压缩后模型输入必须包含当前委派与当前有效任务。

## 5. 交互会话保存与恢复（2026-10-09）

会话是持久化的 Lead 对话上下文，可包含多轮问答；从用户第一次有效交互时创建。

- 每个会话一个权威文件：`~/.agent-harness/sessions/<session_id>.json`，固定位置使任意启动目录都能按 ID 查找，
  不维护索引。只维护一种 JSON 格式，不保存版本号。
- 内容包含会话 ID、创建/更新时间、对话消息、交互事件、目标状态与目标事件；读取时校验必要字段。
  缺少必要字段、目录移动后恢复或结构不符时报错并保留原文件，不转换旧文件。
- 保存用同目录临时文件加 `os.replace` 原子替换；所有历史追加、原地替换和压缩替换都经过统一保存入口。
  保存失败时显示原因、当前交互继续，并在后续保存边界重试（dirty 重试）。
- `/resume` 无参数时扫描 `sessions/` 按最近更新时间倒序列出有限条目；`/resume <id>` 读取并验证文件后，
  确认当前回合与后台任务已结束，保存当前上下文、切回原启动目录、恢复目标历史/目标状态/目标事件并设为当前会话。
- 恢复不请求模型；旧子 agent 线程、后台线程与 MCP 连接不恢复或重启。
- `/delete <id>` 删除指定的非活动会话；活动会话不能删除。读不出来的文件也在 `/resume` 列表中，可直接删除。
- 会话文件保存规范化启动目录绝对路径、Lead 活动任务 ID、会话工作树名称（无则 `null`）与 Todo；
  恢复时在原启动目录校验任务归属、状态、工作树绑定与目录有效性，失效则拒绝切换且当前上下文不变。
- 任务完成后清除任务分配但保留会话工作树；只有未关联工作树的会话使用启动目录。
- goal 状态恢复：当前活动条件、设置时间、评估次数、最近原因、连续阻止次数与最近状态；不恢复 token 起点或消耗。

## 6. Textual 界面（2026-10-09）

默认入口保持 CLI，新增 `agent-harness tui` 与源码入口；Textual 只作为可选依赖（`tui` extra，
`textual>=8.2.8,<9`），普通 CLI 与离线 workflow 不加载它。

- 核心执行沿用同步代码与现有线程：单 Lead 执行线程、CLI 唯一标准输入读取线程、Textual UI 线程；不做核心异步化。
- `runtime/interactive_runtime.py` 的 `InteractiveRuntime` 是 CLI/TUI 共用驱动，interface 为
  `start()` / `submit(text)` / `request_stop(turn_id)` / `close()`；停止与关闭只发请求，完成经事件通知。
- `runtime/interaction.py` 提供事件信封、`TurnOutcome`、提交结果、执行上下文、线程安全事件通道与一次性审批 rendezvous；
  审批请求带唯一 `request_id`、会话代次、执行来源、工具名与待确认参数。
- 审批必须等此前相关回合消息与工具调用展示完成后才交付前端；`InteractionChannel` 集中该条件，两个前端只报告展示完成。
- CLI 主线程是唯一 stdin 读取者，worker 只发布审批请求并等待响应；回合或命令真正结束后才恢复普通提示符。
- 停止是合作式请求，不中断进行中的同步网络调用；host 在模型请求前后、每个工具开始前及审批返回后检查停止标志。
  停止不批量终止其他子 agent、workflow 或后台任务，也不触发 goal 自动续做。
- 关闭顺序：标记 closing → 停止当前回合并结束审批等待 → 等待执行到达停止边界（不在 UI 线程阻塞获取 `agent_lock`）
  → 串行保存会话并报告结果、停止 cron 与后台 Lead 驱动、请求 persistent 子 agent 关闭 → 清理 shell 并等待有归属线程退出
  → 发布一次 `runtime_closed` 后释放 adapter。
- 命令补全：输入框键入 `/` 出现候选，前缀过滤、上下键选择、回车执行；`OptionList` 不获取焦点，App 转发导航。
  Esc 隐藏草稿保留，下次编辑重开；菜单只产生提交文本，业务由 runtime 执行。
- 命令目录统一在 `runtime/local_commands.py`，当前为 `/goal`、`/new`、`/resume`、`/delete`、`/quit`。
  菜单只展示已实现能力，不补全会话 ID、路径、工具名或 skills。
- TUI 当前为 CodeFox 工作台：顶部品牌栏与入口、左侧对话区、右侧只读会话状态（默认收起）、底部输入区；
  同次模型调用按关联 ID 分组，摘要默认可见、详情可展开；Enter 发送，Shift+Enter 换行。
- 展示记录与恢复的语义：实时事实接受时交付并进入保存队列，检查点只消费和保存不重新交付；
  正文片段只用于实时显示，正式回复接受后才保存；恢复时未结束操作显示“上次中断，结束状态未记录”。

## 7. CodeFox 工作台前端（2026-10-10）

在现有 Textual 前端上深化，目标是会话历史、恢复、开启新会话、左侧对话上下文、右侧会话状态与交互输入都在一个工作台完成；
除“开启新会话”所需的最小运行时操作外复用现有后端。

- 顶部三个固定入口：`会话历史`（保存会话列表，恢复/删除）、`会话状态`（切换右侧只读面板显隐）、`开启新会话`（保存当前会话并创建干净新会话）。
- `/new` 是运行时命令：保存当前会话并重置运行时上下文，前端不靠清空消息组件伪造新会话。
- 右侧会话状态默认收起，入口是顶部栏右侧的 `会话状态` 按钮；展开后在主体右侧占 32 列并自带 `[收起]`，再点 `会话状态` 收起。
  状态只显示后端已提供的字段：会话 ID 与状态、消息数量、goal、TODO、工作目录/任务 ID/工作树、活跃子 agent、模型与用量。
- 品牌栏固定 1 行高，左侧为狐狸头像与 `CodeFox`（SVG 缺失时退回纯字符表现），右侧三个入口各 1 行高；不含装饰符号。
- 顶部入口和 `会话状态` 切换后焦点回到输入框，草稿不丢；状态面板收起后仍可由 `会话状态` 重新展开。
- 底部输入区：主按钮在“发送/停止”间切换，只有这两个文案；停止后按钮禁用并保持“停止”，收到 `turn_finished` 才恢复“发送”。
  Enter 发送、Shift+Enter 换行（显式按键处理，不依赖 Textual 默认）；Ctrl+Enter 发送、Ctrl+L 清空、Ctrl+C 停止、Ctrl+D 退出；粘贴多行不触发发送。
  底部摘要固定 1 行，模型名过长时从行尾截断；底部“消息”与状态面板“消息”取同一计数来源。
- 输入框上方显示运行时状态（就绪/处理中/关闭中，只来自 `busy`/`closing`，停止请求不新增状态）；
  下方左侧是权限摘要（`按工具策略` / `等待审批`），右侧是模型名、消息数量与字符预算 `core.CONTEXT_LIMIT`。
  命令失败只弹窗提示，状态行回到就绪；错误正文仍由展示记录留在对话区。
- 同次模型调用的执行事件套一层调用外框：默认收起为一行 `运行中 <本次操作> · 已耗时 Ns`，完整操作原样透传不截断；
  单击展开为 `已运行的命令` 分组头加若干 `已运行 <本次操作>` 行；单个事件行用“进行中”，外框收起行用“运行中”，两者不混用。
- 进行中事件首次显示即带耗时文案（事件行与调用外框都在首帧计算），前端计时器只刷新已存在的进行中显示，
  结束后固定为后端确认的总耗时。
- 消息框内不显示“你”“CodeFox”等来源标签；用户消息记录行右对齐、模型回复和事件行左对齐，事件框保留来源名。
  用户气泡按内容收缩并贴右（`width: auto` + `max-width: 82%`），短消息贴近右边缘、长消息在 82% 处换行；
  提交的待确认用户消息立即按同一右对齐行挂载，后端记录到达时复用该行，不出现先左后右跳动。
- 会话状态面板的 TODO 用中文状态标签 `[完成] …`，与对话区事件框共用一份标签。
- 会话历史面板：标题 `会话历史`、标题行右侧 `[返回]`、底部 `[恢复所选] [删除所选] [刷新]`，刷新重新提交 `/resume` 取列表。
  点击列表行只改变高亮，不触发恢复；恢复/删除必须点对应按钮。
  当前会话在列表中标注“（当前会话 · 正在占用）”，删除被拒并弹窗提示。
  读不出来的会话文件也列进列表并标注“（无法读取）”，可直接删除，恢复被拒并弹窗提示。
  面板按钮都是 1 行紧凑按钮，标题行的 `[返回]` 不会被裁切；列表上方一行只写用户能用的事实
  （无法读取的文件数），没有异常时留空，不显示原始解析错误。
- 不纳入：思考强度选择、权限模式切换、模型切换/API 配置/主题/插件/通用设置存储、新 Logo；这些后端没有对应能力。

## 8. 运行期职责深化（2026-10-10）

本阶段含同日的前一轮“整体架构职责收敛”与其后的“整体运行期职责深化”；同一事实在本文只有一处。

职责收敛（工具结果、Lead 上下文与 host 入口）：

- 工具在内部返回正文与明确执行状态（`ToolResult.status`），模型继续收到文本结果；失败由 Provider 按协议表达。
- 运行时持有唯一 `LeadContext`（`runtime/lead_context.py`）：Todo、会话 ID 与保留工作树归它所有；
  任务板只维护任务文件、依赖、认领、assignment 及其版本，不再保存会话 Todo 全局或保存回调。
- host 聚合重导出删除后，调用方直接导入真实职责 module。
- 失败结算策略：工具已成功、PostToolUse 失败 → 先记录并保存真实工具结果，单独记录 Hook 失败，然后停止当前执行，
  不把工具改成失败、不重跑；PreToolUse 抛异常 → 停止当前执行，handler 与 PostToolUse 不运行；
  普通工具失败 → 生成配对失败结果，同批其他工具按现有顺序继续；部分操作后失败 → 整体请求失败，
  正文列出已发生事实，保留资源与副作用，不自动重试或回滚。

工具调用协议与 Todo 边界：

- Provider 把 API 已标识的工具调用转成 `Block(type="tool_use", id, name, input=dict)`，同步与流式都遵守；
  普通文本中的 JSON 不作为工具调用执行。
- 协议解析失败（参数不完整、顶层非对象、调用未完整返回）抛 `ProtocolResponseError`，该响应整批不执行、不自动重试；
  Todo 参数检查失败（缺字段、类型或状态不符）只让该次调用返回错误 `tool_result`，原列表不变。
- `todo_write` 只接收协议层已解析的列表，检查每项为对象、`content` 为字符串、`status` 属于
  `pending`/`in_progress`/`completed`；通过后整份替换列表、发布 `todo_updated` 并保存检查点，返回清单统计。
  Todo 由模型明确提交；普通工具不自动改变 Todo，主循环不注入提醒。

运行期职责深化：

- 会话切换由 `runtime/session_switch.py` 的 `SessionSwitch.switch(target)` 独占：读快照、校验、停服务、chdir、
  激活归属、重启服务、发布 `session_changed` 都在实现内按序完成，失败回滚也在实现内。
- 命令路由由 `runtime/commands.py` 的 `CommandRouter.dispatch(text)` 处理本地命令并返回 `CommandOutcome`；
  `/resume` 委托 `SessionSwitch`。
- 回合执行由 `runtime/turn_runner.py` 的 `TurnRunner.run(submission_id, kind, text)` 拥有：接受到终态的事件、
  `_pending`/`_busy`/`_turn`/`_stop`、后台暂停与 lead 用量账本。
- `InteractiveRuntime` 只保留提交排队、生命周期标志、线程启动与关闭编排。
- 子 agent 操作成为 Lead interface，模块级全局降为私有；锁序由单一私有 `_team_lock()` 上下文管理器强制，
  嵌套点统一经它进入；提供 `has_pending_inbox()` 与 `active_names()` 只读 interface。
- 回合由 `agents/lead_turn.py` 的 `LeadTurn` 唯一拥有：会话历史、展示发布、checkpoint、`model_call_id`、
  cron 待交付；interface 含 `history` 与 `inject_background_notes` / `after_compaction` /
  `after_manual_compaction` / `after_goal_evaluation`。`host.agent_loop(history, ...)` 退化为顺序调用与循环控制。
- 工具结果契约在 `tools/tool_result.py`：`ToolResult` 携带不可变正文与状态
  （`returned`/`success`/`failed`/`denied`/`stopped`/`started`），状态不靠正文猜测。
- `frontends/conversation.py` 的 `ConversationProjection` 是共用展示投影：记录去重、工具关联、正文确认与作废；
  返回独立数据，不操作终端或控件。
