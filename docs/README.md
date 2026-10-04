# 文档索引

`docs/` 保存项目级设计与执行记录。代码细节紧邻代码或测试，个人操作笔记放 `note/`，不在这里重复。

| 目录/文件 | 内容 | 状态 |
| --- | --- | --- |
| [plans/2026-10-04-agent-harness-执行计划.md](plans/2026-10-04-agent-harness-执行计划.md) | 项目合并与结构演进的决策和结果；含结构规范化与 `src/` 布局记录 | 有内容 |
| `adr/` | 架构决策记录，`<四位序号>-<短名称>.md` | 空，预留 |
| `plans/` | 有明确交付与完成条件的路线、里程碑 | 有内容 |
| `research/` | 外部一手来源的调研笔记 | 空，预留 |
| `handoff/` | 会话交接记录 | [2026-10-04 结构初始化](handoff/2026-10-04-structure-init-handoff.md) |
| `guides/` | 可复现的安装、导入和操作说明 | 空，预留 |
| `agents/` | agent 协作约定 | [issue-tracker.md](agents/issue-tracker.md)、[triage-labels.md](agents/triage-labels.md)、[domain.md](agents/domain.md) |

## 命名规则

- 架构决策用 `adr/<四位序号>-<短名称>.md`，序号从 `0001` 起。
- 计划用描述目标的短名称，可带日期前缀。
- 交接用 `<日期>-<主题>-handoff.md`，放在 `handoff/`。
