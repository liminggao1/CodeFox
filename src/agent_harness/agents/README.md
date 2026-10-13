# agents：agent 执行与控制

提供子 agent 生命周期、共享执行回合，以及 Lead 的回合记录、工具批次、目标控制和错误恢复。
该目录的职责不限于子 agent；Lead 主循环仍在根目录的 `host.py`。

| 文件 | 职责 |
| --- | --- |
| [lead_turn.py](lead_turn.py) | `LeadTurn`：接受的模型历史、流式调用与正式消息关联、逐项工具结果与检查点、cron 交付登记，以及完成/失败/停止/截断的收尾 |
| [lead_tools.py](lead_tools.py) | `execute_tool_batch`：Lead 的 compact 特殊路径、普通工具与后台派发；逐项记录结果，权限检查与执行结算分别委托运行时 |
| [subagent.py](subagent.py) | `spawn_subagent` 统一创建 once/persistent 子 agent；维护生命周期、信箱和协议投递 |
| [subagent_protocol.py](subagent_protocol.py) | 唯一计划门禁与计划/关闭请求状态；集中提交、审核、回复校验、任务版本失效与清理 |
| [subagent_session.py](subagent_session.py) | `SubagentSession`：两种模式共用的模型回合、提示词状态、工具能力选择与白名单、执行门禁和历史压缩；共用定义与身份绑定委托 tools |
| [goal.py](goal.py) | `GoalController`：目标状态、事件与 Stop 边界的继续/结束决定 |
| [goal_evaluator.py](goal_evaluator.py) | `PromptGoalEvaluator`：无工具模型评估、对话转录与结果校验 |
| [recovery.py](recovery.py) | `RecoveryState`：Lead 模型调用的重试、错误分类及恢复状态 |

[host.py](../host.py) 接入 goal 和 recovery，并调用子 agent 的创建和通信能力。
`host.agent_loop` 保留上下文准备、模型重试/续写、goal 判定和迭代顺序。`LeadTurn` 保存本轮关联状态，调用运行时提供的检查点；工具批次不请求模型，也不决定回合是否结束。
[workflows/](../workflows/README.md) 复用一次性子 agent，不另建一套子 agent 执行器。
`SubagentSession` 从 [shared_tools.py](../tools/shared_tools.py) 取得共用定义，保留模式、任务条件与白名单选择；前置检查委托 [hooks.py](../runtime/hooks.py)，执行结算委托 [tool_execution.py](../runtime/tool_execution.py)。
协议模块使用 subagent 传入的既有 RLock，不拥有线程、信箱或任务板。任务身份由 tasks 一致查询后传入；审批决定与收到回复后的门禁分别维护。

该目录负责执行机制，不渲染 UI，不保存交互会话文件，也不维护任务板或 Git 注册表。
任务与目录操作委托给 [workspace/](../workspace/README.md)，事件和交互审批走 [runtime/interaction.py](../runtime/interaction.py)。

包级结构见 [上级 README](../README.md)。
