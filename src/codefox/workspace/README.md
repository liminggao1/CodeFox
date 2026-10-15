# workspace：任务与工作目录

管理任务板、执行者任务分配、Git 工作树，以及它们之间的绑定。

| 文件 | 职责 |
| --- | --- |
| [tasks.py](tasks.py) | 任务持久化、依赖、认领/完成、assignment；按 LeadContext 查询会话归属，校验任务关联并解析执行目录 |
| [worktrees.py](worktrees.py) | 工作树名称/路径、Git 调用和注册表校验 |
| [task_worktrees.py](task_worktrees.py) | 任务与工作树绑定的创建/移除协调；检查占用、处理分配，移除时保留分支 |

[runtime/](../runtime/README.md) 在会话恢复时使用这里的校验和激活能力。
宿主、子 agent 和文件工具通过任务分配确定实际工作目录。
具体操作通过根目录 [registrations.py](../registrations.py) 注册为模型工具。

任务板与工作树状态跟随会话启动目录。
`worktrees.py` 处理 Git 与注册表，涉及任务状态的跨模块操作放在 `task_worktrees.py`。
这里不拥有交互会话文件、模型历史或子 agent 线程；相关调用由共享驱动和宿主协调。
Lead 的 Todo、会话 ID 和保留工作树由 [LeadContext](../runtime/lead_context.py) 持有；本目录不保存会话或注册保存回调。

包级结构见 [上级 README](../README.md)。
