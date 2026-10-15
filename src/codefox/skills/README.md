# skills：随包技能

这里保存提供给模型的技能指令和配套资源。每项技能以自己的 `SKILL.md` 为入口。

| 目录 | 内容与入口 |
| --- | --- |
| [agent-builder/](agent-builder/SKILL.md) | Agent 设计与实现指引，附参考代码和初始化脚本 |
| [code-review/](code-review/SKILL.md) | 代码审查步骤与检查项 |
| [mcp-builder/](mcp-builder/SKILL.md) | MCP 服务开发指引 |
| [pdf/](pdf/SKILL.md) | PDF 读取、生成与处理指引 |

## 加载约定

加载器只扫描本目录直属子目录中的 `SKILL.md`。文件头的 YAML 提供 `name` 和 `description`，正文保存技能指令。
扫描结果进入系统提示词中的技能目录；模型调用 `load_skill(name)` 时取得对应文件全文。
配套的 `references/` 和 `scripts/` 由技能正文说明如何使用，扫描目录不会自动执行脚本。

加载逻辑见 [context/](../context/README.md)，资源打包规则见仓库根目录的 [pyproject.toml](../../../pyproject.toml)。
包级职责见 [上级 README](../README.md)。
