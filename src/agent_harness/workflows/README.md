# workflows：程序化工作流

用程序组织步骤与 agent 调用，记录结果和消费，支持恢复已完成步骤。
工作流恢复复用 journal，交互会话恢复由 [runtime/](../runtime/README.md) 管理。

| 文件 | 职责 |
| --- | --- |
| [workflow_runtime.py](workflow_runtime.py) | 工作流注册、schema 校验、执行状态、`ctx.agent/parallel/pipeline/workflow`、预算、journal、锁和恢复；提供 `WorkflowTool` 与执行入口 |
| [workflow_demo.py](workflow_demo.py) | 示例工作流和 schema、`DemoModelClient`、显式注册函数，以及 CLI 离线演示/恢复 |

[host.initialize_runtime](../host.py) 显式注册示例并将工作流接入工具清单，不覆盖调用方已选的 runner。
默认 `SubagentRunner` 构造时读取当前 client/model；[frontends/cli.py](../frontends/cli.py) 的 `workflow` 路由显式注册示例并选择离线 client。
单独导入 `workflow_runtime.py` 不会自动注册 demo。

工作流 agent 步骤通过 [agents/subagent.py](../agents/subagent.py) 的统一入口执行 once 子 agent。
工作流负责步骤组织、预算、格式重试和缓存恢复；模型与工具回合复用子 agent 执行能力。
该目录不实现 CLI/TUI 交互会话，也不另建子 agent 生命周期。

包级结构见 [上级 README](../README.md)。
