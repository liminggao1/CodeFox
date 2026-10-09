# tools：工具实现与注册机制

提供基础工具、MCP 接入和工具注册机制。具体开放哪些工具由宿主装配。

| 文件 | 职责 |
| --- | --- |
| [file_tools.py](file_tools.py) | bash、文件读/写/改、glob 和 TODO；路径约束、执行者目录处理和 handler 调用 |
| [mcp_tools.py](mcp_tools.py) | MCP 客户端、连接、名称规范化、动态工具定义和 handler 接入 |
| [tool_registry.py](tool_registry.py) | `ToolDefinition`、`ToolSnapshot`、`ToolRegistry` 和 `REGISTRY`；统一工具 schema、handler 与权限快照 |

工具的具体 schema 和 handler 清单在根目录 [registrations.py](../registrations.py)。
[host.py](../host.py) 配置注册表，再向模型开放工具并调度调用。
工具权限和审批接入 [runtime/hooks.py](../runtime/hooks.py)，不在前端重建一套检查。

任务板和工作树操作由 [workspace/](../workspace/README.md) 实现，工具层通过装配暴露这些操作。
这里不运行 Lead 主循环，也不决定交互命令或工作流预算。

包级结构见 [上级 README](../README.md)。
