# providers：模型协议适配

将外部模型 API 的请求、响应和错误转换为项目执行代码使用的形式。

| 文件 | 职责 |
| --- | --- |
| [llm_client.py](llm_client.py) | `Anthropic` 客户端工厂；Anthropic Messages / OpenAI Responses 选择、请求转换、响应解析和协议错误 |
| [message_content.py](message_content.py) | `extract_text` 与 `has_tool_use`，统一读取消息块中的文本和工具请求 |

根目录 [core.py](../core.py) 创建配置好的客户端，Lead、子 agent、目标评估和记忆请求使用它。
`message_content.py` 为宿主、子 agent 和压缩代码提供内容读取能力。

本目录负责协议有效性，不决定重试次数、任务调度或会话生命周期。
Lead 的错误恢复状态在 [agents/recovery.py](../agents/recovery.py)。
配置与模型常量在 `core.py`；包级结构见 [上级 README](../README.md)。
