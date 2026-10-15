# context：模型上下文

准备模型调用所需的提示词、历史、记忆和技能内容。

| 文件 | 职责 |
| --- | --- |
| [prompts.py](prompts.py) | 固定提示词分段与 `assemble_system_prompt`，拼装身份、启动环境、工具规则和技能目录 |
| [prompt_state.py](prompt_state.py) | 固定 system 快照、追加式运行通知、状态校验与压缩后的完整状态刷新；行为见仓库事实第 10 节 |
| [compaction.py](compaction.py) | 工具结果瘦身、历史裁剪、摘要、手动/自动压缩及超限后的被动恢复 |
| [memory_runtime.py](memory_runtime.py) | 记忆文件读写、索引、相关记忆选取、抽取、去重与整理 |
| [skills.py](skills.py) | 扫描 `SKILL.md`、维护技能索引，提供 `list_skills` 和 `load_skill` |

根目录 [runtime_context.py](../runtime_context.py) 从记忆等状态构造每次请求的上下文快照，
调用方将上下文字段交给共享 [TurnEngine](../agents/turn_engine.py)，由它调用 `PromptState.prepare()`。
[LeadTurn](../agents/lead_turn.py) 与 [SubagentSession](../agents/subagent_session.py) 各自持有提示词状态和恢复策略，
并使用这里的历史压缩能力；Lead 回合结束时由宿主侧处理记忆。

`skills.py` 是加载代码，资源目录是包根目录的 [skills/](../skills/)。
资源路径由 `core.SKILLS_DIR` 提供，不能改成相对于本子包查找。
记忆跟随会话启动目录；会话 JSON 保存由 [runtime/session_store.py](../runtime/session_store.py) 负责。

该目录处理上下文，不组织前端交互或子 agent 生命周期。包级结构见 [上级 README](../README.md)。
