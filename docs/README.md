# docs：项目文档

`docs/` 保存项目级架构、计划、调研、交接和协作约定。
本页只说明文档层的用途和直属目录；某个目录里的文件清单与写作规则由该目录的 README 维护。

代码细节放在代码目录的 README 或测试说明中，个人环境和操作笔记放在 [note/](../note/README.md)。
项目运行方式见 [根目录 README](../README.md)，术语见 [GLOSSARY.md](../GLOSSARY.md)。

## 目录

| 目录 | 负责内容 | 入口 |
| --- | --- | --- |
| `architecture/` | 当前架构、跨模块行为事实和生成的依赖图 | [architecture/README.md](architecture/README.md) |
| `adr/` | 架构决策记录 | [adr/README.md](adr/README.md) |
| `plans/` | 历史实施计划、路线和待验证事项 | [plans/README.md](plans/README.md) |
| `research/` | 外部资料和技术调研 | [research/README.md](research/README.md) |
| `handoff/` | 独立会话的交接和验收记录 | [handoff/README.md](handoff/README.md) |
| `guides/` | 可复现的专题操作指南 | [guides/README.md](guides/README.md) |
| `agents/` | 本仓库的 agent 协作约定 | [agents/README.md](agents/README.md) |

## 文档归属

- 当前行为和跨模块约束以 `architecture/facts.md` 为准。
- 实施计划记录当时的目标、过程和验收，不替代当前架构事实。
- 交接记录保存一次会话的背景和结果；稳定规则应回写到架构事实或代码目录说明。
- 同一事实只保留一个权威来源，其他文档通过链接引用。

## 命名

- 架构决策：`adr/<四位序号>-<短名称>.md`。
- 计划：`plans/<日期>-<两位顺序号><主题>.md`。
- 交接：`handoff/<日期>-<主题>-handoff.md`。
