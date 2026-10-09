# 文档索引

`docs/` 保存项目级设计与执行记录。代码细节紧邻代码或测试，个人操作笔记放 `note/`，不在这里重复。
领域术语见 [GLOSSARY.md](../GLOSSARY.md)，运行方式和代码布局见 [README.md](../README.md)。

项目面向可长期演进的本地 agent 运行环境，保留主循环、工具、子 agent、工作流和目标控制的完整机制。
项目当前不做打包发布。
当前验证包括离线模型驱动的持久子 agent、跨任务目录与历史、通信和关闭，以及两种子 agent 模式的端到端自检；真实 API 交互问答和 memory 抽取尚未实测。

| 目录/文件 | 内容 | 状态 |
| --- | --- | --- |
| [plans/2026-10-04-agent-harness-执行计划.md](plans/2026-10-04-agent-harness-执行计划.md) | 项目合并与结构演进的决策和结果；含结构规范化与 `src/` 布局记录 | 有内容 |
| [plans/2026-10-06-架构调整计划.md](plans/2026-10-06-架构调整计划.md) | 模块职责、依赖与调试成本的分阶段调整计划 | 已实施 |
| [plans/2026-10-08-架构深化实施计划.md](plans/2026-10-08-架构深化实施计划.md) | memory、context、工具装配、队友、workflow 与 host facade 的改造与验收 | 已实施 |
| [plans/2026-10-08-第二轮架构深化实施计划.md](plans/2026-10-08-第二轮架构深化实施计划.md) | 响应有效性、历史压缩、后台结果与 Lead 恢复状态的改造与验收 | 已实施 |
| [plans/2026-10-08-统一子agent实施计划.md](plans/2026-10-08-统一子agent实施计划.md) | 统一创建与生命周期、复核修正及工作流接入扩展 | 阶段 0–6 已实施 |
| [plans/2026-10-09-交互会话保存与恢复计划.md](plans/2026-10-09-交互会话保存与恢复计划.md) | CLI/TUI 上下文保存、`/resume` 恢复、`/delete` 清理和任务、工作目录关联 | 两阶段已实施 |
| [plans/2026-10-09-Textual交互界面实施计划.md](plans/2026-10-09-Textual交互界面实施计划.md) | Textual 界面、共享交互驱动、事件、审批与关闭协议 | 阶段一至七已实施；真实 API 的 TUI 尚未实测 |
| `adr/` | 架构决策记录，`<四位序号>-<短名称>.md` | 空，预留 |
| `plans/` | 有明确交付与完成条件的路线、里程碑 | 有内容 |
| `research/` | 外部一手来源的调研笔记 | 空，预留 |
| `handoff/` | 独立会话的交接记录；计划实施和验收结果写回对应计划 | [2026-10-04 结构初始化](handoff/2026-10-04-structure-init-handoff.md) |
| `guides/` | 可复现的安装、导入和操作说明 | 空，预留 |
| `agents/` | agent 协作约定 | [issue-tracker.md](agents/issue-tracker.md)、[triage-labels.md](agents/triage-labels.md)、[domain.md](agents/domain.md) |

## 命名规则

- 架构决策用 `adr/<四位序号>-<短名称>.md`，序号从 `0001` 起。
- 计划用描述目标的短名称，可带日期前缀。
- 交接用 `<日期>-<主题>-handoff.md`，放在 `handoff/`。
