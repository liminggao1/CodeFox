# architecture：架构事实

这里维护当前架构和跨模块行为的说明。需要确认“系统现在应该怎样工作”时，先读 `facts.md`。

| 文件 | 内容 |
| --- | --- |
| [facts.md](facts.md) | 当前架构、权限、会话、子 agent、工作流和上下文等行为事实；跨模块规则的唯一权威 |
| [deps.md](deps.md) | 面向人阅读的 Mermaid 依赖图和图例 |
| [deps.mmd](deps.mmd) | 纯 Mermaid 依赖图，供工具和 AI 读取 |

`deps.md` 和 `deps.mmd` 是生成产物，不手工修改。仓库根目录运行：

```sh
python scripts/arch_views.py
```

依赖图生成逻辑在 [scripts/README.md](../../scripts/README.md)，源码职责从 [src/codefox/README.md](../../src/codefox/README.md) 开始阅读。
本目录不保存实施计划、调研过程或个人调试记录。
