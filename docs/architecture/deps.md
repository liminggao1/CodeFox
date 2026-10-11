# 模块依赖图

由 `scripts/arch_views.py` 生成，请勿手改。重新生成：

```bash
python scripts/arch_views.py
```

## 图例 · 线的含义

| 线型 | 含义 | 说明 |
| --- | --- | --- |
| 深色实线 | 跨层依赖 | 架构主干 |
| 浅灰细线 | 同层依赖 | 弱化显示 |
| 红色实线 | 回边 | 指向层序上方的反馈边；删掉这些边后，其余依赖可排成无环的层序 |

## 节点颜色

蓝=入口，黄=装配，绿=核心，紫=底层。

## 循环依赖（需收敛）

下面每个强连通分量是一个循环；红线是用贪心法挑出的反馈边集合（不保证最少），收敛目标就是消除这些回边。

- agents ↔ context ↔ core ↔ host ↔ registrations ↔ runtime ↔ runtime_context ↔ tools ↔ workflows ↔ workspace

节点是 `agent_harness` 的子包与顶层模块，箭头 `A --> B` 表示 A 导入 B。
全部依赖都在图上，没有省略；同层边只是弱化，不会藏住问题。
VS Code 中打开本文件按 `Ctrl+Shift+V` 预览；AI 读同目录的纯文本 `deps.mmd`。

```mermaid
graph TD
    classDef 入口 fill:#dbeafe,stroke:#1e40af,color:#1e40af
    classDef 装配 fill:#fef3c7,stroke:#92400e,color:#92400e
    classDef 核心 fill:#dcfce7,stroke:#166534,color:#166534
    classDef 底层 fill:#f3e8ff,stroke:#6b21a8,color:#6b21a8
    classDef 其他 fill:#e2e8f0,stroke:#475569,color:#334155
    agents --> context
    agents --> core
    agents --> providers
    agents --> runtime
    agents --> tools
    agents --> workspace
    context --> core
    context --> providers
    context --> runtime
    core --> providers
    core --> runtime
    frontends --> core
    frontends --> runtime
    frontends --> workflows
    host --> agents
    host --> context
    host --> core
    host --> providers
    host --> registrations
    host --> runtime
    host --> runtime_context
    host --> tools
    host --> workflows
    host --> workspace
    registrations --> agents
    registrations --> context
    registrations --> core
    registrations --> runtime
    registrations --> tools
    registrations --> workspace
    runtime_context --> agents
    runtime_context --> context
    runtime_context --> tools
    tools --> core
    tools --> runtime
    tools --> workspace
    workflows --> agents
    workflows --> core
    workflows --> runtime
    workflows --> tools
    workspace --> core
    workspace --> runtime
    runtime --> agents
    runtime --> context
    runtime --> core
    runtime --> host
    runtime --> tools
    runtime --> workspace
    class agents 核心
    class context 核心
    class core 装配
    class frontends 入口
    class host 装配
    class providers 底层
    class registrations 装配
    class runtime 核心
    class runtime_context 装配
    class tools 核心
    class workflows 核心
    class workspace 核心
    linkStyle 0 stroke:#cbd5e1,stroke-width:1px
    linkStyle 3 stroke:#cbd5e1,stroke-width:1px
    linkStyle 4 stroke:#cbd5e1,stroke-width:1px
    linkStyle 5 stroke:#cbd5e1,stroke-width:1px
    linkStyle 8 stroke:#cbd5e1,stroke-width:1px
    linkStyle 16 stroke:#cbd5e1,stroke-width:1px
    linkStyle 18 stroke:#cbd5e1,stroke-width:1px
    linkStyle 20 stroke:#cbd5e1,stroke-width:1px
    linkStyle 26 stroke:#cbd5e1,stroke-width:1px
    linkStyle 34 stroke:#cbd5e1,stroke-width:1px
    linkStyle 35 stroke:#cbd5e1,stroke-width:1px
    linkStyle 36 stroke:#cbd5e1,stroke-width:1px
    linkStyle 38 stroke:#cbd5e1,stroke-width:1px
    linkStyle 39 stroke:#cbd5e1,stroke-width:1px
    linkStyle 41 stroke:#cbd5e1,stroke-width:1px
    linkStyle 42 stroke:#b91c1c,stroke-width:2px
    linkStyle 43 stroke:#b91c1c,stroke-width:2px
    linkStyle 44 stroke:#b91c1c,stroke-width:2px
    linkStyle 45 stroke:#b91c1c,stroke-width:2px
    linkStyle 46 stroke:#b91c1c,stroke-width:2px
    linkStyle 47 stroke:#b91c1c,stroke-width:2px
```
