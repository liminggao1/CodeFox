# tools：工具实现与注册机制

提供基础工具、MCP 接入和工具注册机制。具体开放哪些工具由宿主装配。

| 文件 | 职责 |
| --- | --- |
| [file_tools.py](file_tools.py) | bash、文件读/写/改、glob 和 TODO；路径约束、执行者目录处理和明确工具结果 |
| [tool_result.py](tool_result.py) | 不可变的正文与状态契约；按调用 ID 转换模型结果，失败标记独立于正文 |
| [shared_tools.py](shared_tools.py) | Lead/child 共用的八项工具定义、局部 schema 差异、任务身份绑定和任务板文本；每次构造独立 schema |
| [mcp_tools.py](mcp_tools.py) | MCP 客户端、连接、名称规范化、动态工具定义和 handler 接入 |
| [tool_registry.py](tool_registry.py) | `ToolDefinition`、`ToolSnapshot`、`ToolRegistry` 和 `REGISTRY`；统一工具 schema、handler 与权限快照 |

共用工具定义由 `shared_tools` 维护；Lead 清单在根目录 [registrations.py](../registrations.py) 组合，child 模式与白名单由 [subagent_session.py](../agents/subagent_session.py) 选择。
`file_tools.bind_file_tools` 只绑定身份，每次调用通过 `tool_cwd` 解析当前任务目录；后台命令启动时用同一入口固定目录。
[host.initialize_runtime](../host.py) 显式配置注册表，再由主循环向模型开放工具并调度调用；导入 host 不配置应用。
工具权限和审批接入 [runtime/hooks.py](../runtime/hooks.py)，不在前端重建一套检查。

`todo_write` 把已解析的列表交给运行时的 LeadContext 检查并整份替换，发布 `todo_updated`。结果正文返回本次清单统计；运行时在工具结算检查点保存。
Todo 内容和状态由模型明确提交；普通工具不自动改变 Todo，主循环不注入提醒。当前结果契约与状态职责见 [仓库事实](../../../docs/facts.md) 第 8 节。

任务板和工作树操作由 [workspace/](../workspace/README.md) 实现，工具层通过装配暴露这些操作。
这里不运行 Lead 主循环，也不决定交互命令或工作流预算。
共用定义与目录绑定不执行授权，不生成或保存展示记录；执行状态和 TODO 事实仍经原有执行路径交给交互运行时。

包级结构见 [上级 README](../README.md)。
