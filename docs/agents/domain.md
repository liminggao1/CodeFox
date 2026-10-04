# Domain Docs

工程技能在探索代码库时应如何读取本仓库的领域文档。

## Before exploring, read these

- **`GLOSSARY.md`**（仓库根目录）：项目术语表。
- **`docs/adr/`**：读取与当前工作区域相关的 ADR。

这些文件不存在时**静默跳过**，不要声张、也不要主动建议先创建。术语或决策真正确定下来时，
由 `/domain-modeling`（经 `/grill-with-docs` 与 `/improve-codebase-architecture` 到达）按需创建。

## File structure

本仓库采用单上下文：

```
/
├── GLOSSARY.md
├── docs/adr/
│   └── 0001-example-decision.md
└── src/agent_harness/
```

## Use the glossary's vocabulary

输出里提到领域概念时（议题标题、重构提案、假设、测试名），使用 `GLOSSARY.md` 定义的词，
不要漂移到术语表明确回避的同义词。

需要的概念不在术语表里，这是一个信号：要么你在发明项目不用的说法（重新考虑），
要么确实有缺口（记下来交给 `/domain-modeling`）。

## Flag ADR conflicts

输出与已有 ADR 冲突时显式指出，不要默默覆盖：

> _与 ADR-0003（fcntl 跨平台降级）冲突，但值得重开，因为…_
