# agents：agent 执行与控制

提供子 agent 生命周期、共享执行回合，以及 Lead 使用的目标控制和错误恢复。
该目录的职责不限于子 agent；Lead 主循环仍在根目录的 `host.py`。

| 文件 | 职责 |
| --- | --- |
| [subagent.py](subagent.py) | `spawn_subagent` 统一创建 once/persistent 子 agent；维护生命周期、信箱、计划审批和关闭协议 |
| [subagent_session.py](subagent_session.py) | `SubagentSession`：两种模式共用的模型回合、工具能力选择与白名单、执行门禁和历史压缩；共用定义与身份绑定委托 tools |
| [goal.py](goal.py) | `GoalController` 与 `PromptGoalEvaluator`：目标状态、评估与 Stop 边界的继续/结束决定 |
| [recovery.py](recovery.py) | `RecoveryState`：Lead 模型调用的重试、错误分类及恢复状态 |

[host.py](../host.py) 接入 goal 和 recovery，并调用子 agent 的创建和通信能力。
[workflows/](../workflows/README.md) 复用一次性子 agent，不另建一套子 agent 执行器。
`SubagentSession` 从 [shared_tools.py](../tools/shared_tools.py) 取得共用定义，保留模式、任务条件与白名单选择，以及可用性 → 计划门禁 → PreToolUse → handler → PostToolUse 的顺序。

该目录负责执行机制，不渲染 UI，不保存交互会话文件，也不维护任务板或 Git 注册表。
任务与目录操作委托给 [workspace/](../workspace/README.md)，事件和交互审批走 [runtime/interaction.py](../runtime/interaction.py)。

包级结构见 [上级 README](../README.md)。
