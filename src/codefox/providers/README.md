# providers：模型协议适配

将外部模型 API 的请求、响应和错误转换为项目执行代码使用的形式。

| 文件 | 职责 |
| --- | --- |
| [llm_client.py](llm_client.py) | 客户端工厂；本地消息标记剥离与 token 计量、两种协议的请求转换、完整/流式读取、正文片段和最终响应校验 |
| [message_content.py](message_content.py) | `extract_text` 与 `has_tool_use`，统一读取消息块中的文本和工具请求 |

根目录 [core.py](../core.py) 创建配置好的客户端，Lead、子 agent、目标评估和记忆请求使用它。
`message_content.py` 为宿主、子 agent 和压缩代码提供内容读取能力。

本目录负责协议有效性，不决定重试次数、任务调度或会话生命周期。
`stream_message()` 同步交付可见文字并返回完整 Message；Lead 交互调用使用它，内部 create 调用保持完整响应。
Anthropic 使用已有 SDK 并关闭其隐藏重试，Responses 使用现有 HTTP 传输读取 SSE。
没有结束帧、无效工具 JSON 或截断工具批次都不能作为完整响应接受；部分工具参数不交给执行器。
Lead 的错误恢复状态在 [agents/recovery.py](../agents/recovery.py)。
配置与模型常量在 `core.py`；包级结构见 [上级 README](../README.md)。
