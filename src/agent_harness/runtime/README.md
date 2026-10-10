# runtime：交互会话运行时

集中管理 CLI/TUI 共用的交互规则，以及支撑执行的事件、审批、调度和 hooks。
这里的交互运行时与 [workflows/](../workflows/README.md) 的工作流运行时不同。

| 文件 | 职责 |
| --- | --- |
| [interactive_runtime.py](interactive_runtime.py) | `InteractiveRuntime`：接收输入、执行本地命令、串行驱动 Lead 回合、消费后台输入、生成展示记录、统一保存与恢复交付、切换会话、停止和关闭 |
| [interaction.py](interaction.py) | 执行上下文、事件通道、独立事实队列、稳定记录 ID、审批请求及展示确认，关联会话代次和停止状态 |
| [local_commands.py](local_commands.py) | `LocalCommand`、命令元数据和查找；供运行时路由与前端补全共用 |
| [session_store.py](session_store.py) | `SessionStore` 和 `InteractiveSession`：会话格式校验、原子保存、读取/列出/删除及上下文保存与恢复准备 |
| [scheduler.py](scheduler.py) | 后台任务结果、cron 校验/持久化/调度、待消费队列和服务启动/停止 |
| [hooks.py](hooks.py) | 用户输入、工具执行和停止边界的 hooks；权限检查、审批请求、日志及大结果处理；Lead/child 共用前置检查与同步执行 |

前端调用 `InteractiveRuntime`，后者调用根目录 [host.agent_loop](../host.py)。
`start()` 在线程启动前调用 `host.initialize_runtime()`。配置失败可重试，不启动执行线程；重复配置保留已有 goal、runner 与注册器配置。导入模块和构造驱动不执行应用装配。
执行代码通过 `interaction` 发布事件和请求审批，前端通过同一通道消费和确认。
驱动提供只读事实的纯展示转换，通道在接受事实时按顺序交付展示记录，并将同 ID 的副本加入保存队列。
检查点只消费和保存记录，不重新转换或交付；后台线程不写会话文件，前端消费不取走待保存数据。
消息与事实在安全检查点落盘；单项工具结果可以先于模型整批结果保存，恢复使用真实结果修复未完成批次。
审批创建、结束和等待轮询仍调用检查点，保留前置展示确认屏障。
正文片段只用于实时显示，正式回复接受后才保存；模型、工具和实际 hook 生命周期属于可恢复记录。
调度器提供待交付工作，共享驱动决定何时把它交给 Lead。

运行时拥有交互会话的执行与切换规则，不拥有 UI 控件。
`session_store.py` 负责会话存储与恢复准备，目标是否能激活由共享驱动协调。
`InteractiveSession` 按 ID 追加展示记录，维护模型历史与展示历史的不同用途，失败保留内存待重试；`SessionStore` 只校验当前结构及操作文件。
任务和工作树绑定的校验由 [workspace/](../workspace/README.md) 提供，子 agent 生命周期由 [agents/](../agents/README.md) 提供。

包级入口与职责分组见 [上级 README](../README.md)。
