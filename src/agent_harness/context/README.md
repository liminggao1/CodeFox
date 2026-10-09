# context：模型上下文

准备模型调用所需的提示词、历史、记忆和技能内容。

| 文件 | 职责 |
| --- | --- |
| [prompts.py](prompts.py) | 提示词分段与 `assemble_system_prompt`，拼装身份、环境、工具、技能、memory 和 MCP 内容 |
| [compaction.py](compaction.py) | 工具结果瘦身、历史裁剪、摘要、手动/自动压缩及超限后的被动恢复 |
| [memory_runtime.py](memory_runtime.py) | 记忆文件读写、索引、相关记忆选取、抽取、去重与整理 |
| [skills.py](skills.py) | 扫描 `SKILL.md`、维护技能索引，提供 `list_skills` 和 `load_skill` |

根目录 [runtime_context.py](../runtime_context.py) 从记忆等状态构造每次请求的上下文快照，
[host.py](../host.py) 将其用于提示词，并在回合结束时处理记忆。
主循环和 [SubagentSession](../agents/subagent_session.py) 使用这里的历史压缩能力。

`skills.py` 是加载代码，资源目录是包根目录的 [skills/](../skills/)。
资源路径由 `core.SKILLS_DIR` 提供，不能改成相对于本子包查找。
记忆跟随会话启动目录；会话 JSON 保存由 [runtime/session_store.py](../runtime/session_store.py) 负责。

该目录处理上下文，不组织前端交互或子 agent 生命周期。包级结构见 [上级 README](../README.md)。
