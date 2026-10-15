# agents：agent 执行与控制

提供 Lead 与子 agent 共用的工作循环、各自的执行策略、子 agent 生命周期、目标控制和错误恢复。
完整模型—工具执行顺序由 `turn_engine.py` 管理，根目录 `host.py` 只负责装配 Lead 回调。

| 文件 | 职责 |
| --- | --- |
| [lead_turn.py](lead_turn.py) | `LeadTurn`：Lead 的模型恢复与续写策略、cron 交付登记、Goal/memory 操作、压缩检查点和回合收尾 |
| [turn_engine.py](turn_engine.py) | `TurnEngine`：完整工作循环、上下文准备、API 请求快照、历史与消息关联、单工具批次配对、结果事件、合法阶段和终态 |
| [lead_tools.py](lead_tools.py) | `execute_tool`：Lead 的单工具入口，处理 compact、普通工具与后台派发；返回 `ToolExecution`，不写历史 |
| [subagent.py](subagent.py) | `spawn_subagent` 统一创建 once/persistent 子 agent；维护稳定身份、生命周期、结果查询、信箱和协议投递 |
| [subagent_protocol.py](subagent_protocol.py) | 唯一计划门禁与计划/关闭请求状态；集中提交、审核、回复校验、任务版本失效与清理 |
| [subagent_session.py](subagent_session.py) | `SubagentSession`：装配共享完整工作循环，维护模型/工具预算、用量、提示词状态、白名单、执行门禁与恢复；工具定义与身份绑定委托 tools |
| [goal.py](goal.py) | `GoalController`：目标状态、事件与 Stop 边界的继续/结束决定 |
| [goal_evaluator.py](goal_evaluator.py) | `PromptGoalEvaluator` / `JevGoalEvaluator`：无工具目标评估、对话转录与结构化结果校验；Jev confidence 随目标状态事件记录 |
| [recovery.py](recovery.py) | `RecoveryState`：Lead 模型调用的重试、错误分类及恢复状态 |

[host.py](../host.py) 接入 goal 和 recovery，并调用子 agent 的创建和通信能力。
`host.agent_loop` 与 `SubagentSession.run` 都通过 `TurnCallbacks` 装配 `TurnEngine`。
后者执行完整委派；`subagent.py` 只协调工作开始、信箱、计划等待、空闲与结果交付。
持久子 agent 在接受最终回答前再收一次信；完成边界前收到的消息续用当前预算，之后的消息排入下一项工作。
请求快照、工具配对和阶段隔离的跨模块契约见 [仓库事实第 8 节](../../../docs/architecture/facts.md#8-运行期职责深化2026-10-10)。
[workflows/](../workflows/README.md) 复用一次性子 agent，不另建一套子 agent 执行器。
`SubagentSession` 从 [shared_tools.py](../tools/shared_tools.py) 取得共用定义，保留模式、任务条件与白名单选择；前置检查委托 [hooks.py](../runtime/hooks.py)，执行结算委托 [tool_execution.py](../runtime/tool_execution.py)。
协议模块使用 subagent 传入的既有 RLock，不拥有线程、信箱或任务板。任务身份由 tasks 一致查询后传入；审批决定与收到回复后的门禁分别维护。

该目录负责执行机制，不渲染 UI，不保存交互会话文件，也不维护任务板或 Git 注册表。
任务与目录操作委托给 [workspace/](../workspace/README.md)，事件和交互审批走 [runtime/interaction.py](../runtime/interaction.py)。

包级结构见 [上级 README](../README.md)。
