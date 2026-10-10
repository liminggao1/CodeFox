#!/usr/bin/env python3
"""生成架构视图：模块依赖图，同时服务人和 AI。

扫描 `src/agent_harness` 的 import 关系，按子包与顶层模块聚合，
输出到 `docs/architecture/`：

- `deps.mmd` 纯 Mermaid 文本，AI 可直接读，也可做版本对比
- `deps.md`  把 `deps.mmd` 包进 Mermaid 代码块，并在图外写明线型图例与循环依赖

图只保留模块架构本身；线型图例和循环依赖是说明性内容，放在图的正文里。

只用标准库，无需安装依赖：

    python scripts/arch_views.py

`--stdout` 只打印不写文件；`--out-dir` 可改输出目录。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "agent_harness"
PACKAGE = "agent_harness"
DEFAULT_OUT_DIR = PROJECT_ROOT / "docs" / "architecture"

# 按职责分层配色，帮助读图时区分入口、核心与底层。
LAYERS = {
    "入口": (["frontends"], "#dbeafe", "#1e40af"),
    "装配": (["host", "registrations", "runtime_context", "core"], "#fef3c7", "#92400e"),
    "核心": (["runtime", "agents", "tools", "context", "workspace", "workflows"], "#dcfce7", "#166534"),
    "底层": (["providers"], "#f3e8ff", "#6b21a8"),
}


def iter_source_files():
    """遍历参与建图的 Python 源文件，跳过缓存和随包数据。"""
    for path in sorted(SRC_ROOT.rglob("*.py")):
        parts = path.relative_to(SRC_ROOT).parts
        if "__pycache__" in parts:
            continue
        if parts[0] == "skills":
            continue
        yield path


def collect_nodes():
    """图的节点：agent_harness 的子包与顶层模块，忽略私有入口。"""
    nodes = []
    for child in sorted(SRC_ROOT.iterdir()):
        if child.name.startswith("_"):
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            nodes.append(child.name)
        elif child.suffix == ".py":
            nodes.append(child.stem)
    return nodes


def source_node(path, nodes):
    """源文件所属节点；顶层包入口或未登记的模块返回 None。"""
    head = path.relative_to(SRC_ROOT).parts[0]
    if head.endswith(".py"):
        head = head[:-3]
    return head if head in nodes else None


def absolute_base(package_parts, level, module):
    """把 import 目标还原成绝对模块名；越界的相对导入返回 None。"""
    if level == 0:
        return module or ""
    base = list(package_parts)
    up = level - 1
    if up:
        if up > len(base):
            return None
        base = base[:-up]
    parts = base + ([module] if module else [])
    return ".".join([PACKAGE] + parts) if parts else PACKAGE


def map_module(module_name, nodes):
    """把绝对模块名映射到节点集合，非本包或未登记的模块忽略。"""
    if not module_name or module_name == PACKAGE:
        return set()
    if not module_name.startswith(PACKAGE + "."):
        return set()
    head = module_name[len(PACKAGE) + 1:].split(".", 1)[0]
    return {head} if head in nodes else set()


def imported_nodes(tree, path, nodes):
    """一个源文件导入到的所有节点。"""
    package_parts = list(path.relative_to(SRC_ROOT).parts[:-1])
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(map_module(alias.name, nodes))
        elif isinstance(node, ast.ImportFrom):
            base = absolute_base(package_parts, node.level, node.module)
            if base is None:
                continue
            found.update(map_module(base, nodes))
            for alias in node.names:
                found.update(map_module(base + "." + alias.name, nodes))
    return found


def build_edges(nodes):
    """聚合所有源文件的 import，得到去重后的有向边。"""
    edges = set()
    for path in iter_source_files():
        source = source_node(path, nodes)
        if source is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in imported_nodes(tree, path, nodes):
            if target != source:
                edges.add((source, target))
    return edges


def find_cycles(nodes, edges):
    """Tarjan 求强连通分量，返回大小大于 1 的分量（即循环依赖）。"""
    graph = {name: [] for name in nodes}
    for source, target in edges:
        graph[source].append(target)
    index = {}
    low = {}
    stack = []
    on_stack = set()
    counter = [0]
    cycles = []

    def visit(vertex):
        index[vertex] = low[vertex] = counter[0]
        counter[0] += 1
        stack.append(vertex)
        on_stack.add(vertex)
        for neighbor in graph[vertex]:
            if neighbor not in index:
                visit(neighbor)
                low[vertex] = min(low[vertex], low[neighbor])
            elif neighbor in on_stack:
                low[vertex] = min(low[vertex], index[neighbor])
        if low[vertex] == index[vertex]:
            component = []
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                component.append(popped)
                if popped == vertex:
                    break
            if len(component) > 1:
                cycles.append(sorted(component))

    for name in nodes:
        if name not in index:
            visit(name)
    return sorted(cycles)


def layer_of(node, layers):
    """节点所属层名；未登记的节点归入「其他」。"""
    for name, (members, _, _) in layers.items():
        if node in members:
            return name
    return "其他"


def split_back_edges(nodes, edges):
    """用 Eades 贪心求反馈弧集，返回制造循环的反向边。

    反复剥离无出边/无入边的点，剩下互相依赖的子集时取「出度减入度」最大的点
    优先排出，最后把层序靠后的点指向靠前点的边作为反向边。边数尽量少，
    读图时只需看少量红线，其余边可以一致向下。
    """
    adjacency = {name: set() for name in nodes}
    for source, target in edges:
        adjacency[source].add(target)
    remaining = set(nodes)
    left, right = [], []

    def degrees():
        return {
            name: (
                sum(1 for target in adjacency[name] if target in remaining),
                sum(1 for source in remaining if name in adjacency[source]),
            )
            for name in remaining
        }

    while remaining:
        while remaining:
            degree = degrees()
            sinks = [n for n in sorted(remaining) if degree[n][0] == 0]
            sources = [n for n in sorted(remaining) if degree[n][1] == 0]
            if sinks:
                right.insert(0, sinks[0])
                remaining.discard(sinks[0])
            elif sources:
                left.append(sources[0])
                remaining.discard(sources[0])
            else:
                break
        if not remaining:
            break
        degree = degrees()
        chosen = max(sorted(remaining), key=lambda n: degree[n][0] - degree[n][1])
        left.append(chosen)
        remaining.discard(chosen)

    rank = {name: index for index, name in enumerate(left + right)}
    return {(source, target) for source, target in edges if rank[source] > rank[target]}


def render_mermaid(nodes, edges):
    """渲染 Mermaid 文本：按职责配色，保留全部依赖，只用样式区分主次。

    不删任何边，避免藏住潜在问题；靠视觉权重分出主次：
    - 跨层依赖：深色实线，是架构主干
    - 同层依赖：浅灰细线，数量多但语义弱，弱化后不抢视线
    - 制造循环的边：红色虚线，是循环依赖的根源

    只用颜色分层，不用 subgraph：子图会让连线跨框、布局重叠，反而更难读。
    """
    layers = dict(LAYERS)
    back_edges = split_back_edges(nodes, edges)
    forward = sorted(edge for edge in edges if edge not in back_edges)
    backward = sorted(back_edges)

    # 说明放在 Markdown 包装里，不放 %% 注释：部分 Mermaid 版本会把它当成
    # 语句解析并报错，导致整张图渲染成纯文本。
    lines = ["graph TD"]
    for name, (_, fill, stroke) in layers.items():
        lines.append("    classDef {0} fill:{1},stroke:{2},color:{2}".format(name, fill, stroke))
    lines.append("    classDef 其他 fill:#e2e8f0,stroke:#475569,color:#334155")

    # 逐条声明边，边序号就是 linkStyle 索引，按声明顺序累计。
    same_layer_indexes = []
    for index, (source, target) in enumerate(forward):
        if layer_of(source, layers) == layer_of(target, layers):
            same_layer_indexes.append(index)
        lines.append("    {0} --> {1}".format(source, target))
    back_start = len(forward)
    for source, target in backward:
        lines.append("    {0} -.-> {1}".format(source, target))
    for node in nodes:
        lines.append("    class {0} {1}".format(node, layer_of(node, layers)))
    for index in same_layer_indexes:
        lines.append("    linkStyle {0} stroke:#cbd5e1,stroke-width:1px".format(index))
    for index in range(back_start, back_start + len(backward)):
        lines.append("    linkStyle {0} stroke:#b91c1c,stroke-width:2px,stroke-dasharray:6 4".format(index))
    return "\n".join(lines) + "\n"


def render_markdown(mermaid_text, cycles):
    """把 Mermaid 文本包成可预览的 Markdown，并把线型图例与循环依赖写在图外。

    图只保留模块架构本身；图例和循环依赖是说明性内容，放在图前面的正文里，
    这样图不被额外的框挤宽，说明也不会随图的布局漂移。
    """
    cycle_text = "\n".join(
        "- {0}".format(" ↔ ".join(component)) for component in cycles
    ) if cycles else "- 无"
    return (
        "# 模块依赖图\n"
        "\n"
        "由 `scripts/arch_views.py` 生成，请勿手改。重新生成：\n"
        "\n"
        "```bash\n"
        "python scripts/arch_views.py\n"
        "```\n"
        "\n"
        "## 图例 · 线的含义\n"
        "\n"
        "| 线型 | 含义 | 说明 |\n"
        "| --- | --- | --- |\n"
        "| 深色实线 | 跨层依赖 | 架构主干 |\n"
        "| 浅灰细线 | 同层依赖 | 弱化显示 |\n"
        "| 红色虚线 | 循环依赖 | 需收敛 |\n"
        "\n"
        "## 节点颜色\n"
        "\n"
        "蓝=入口，黄=装配，绿=核心，紫=底层。\n"
        "\n"
        "## 循环依赖（需收敛）\n"
        "\n"
        + cycle_text + "\n"
        "\n"
        "节点是 `agent_harness` 的子包与顶层模块，箭头 `A --> B` 表示 A 导入 B。\n"
        "全部依赖都在图上，没有省略；同层边只是弱化，不会藏住问题。\n"
        "VS Code 中打开本文件按 `Ctrl+Shift+V` 预览；AI 读同目录的纯文本 `deps.mmd`。\n"
        "\n"
        "```mermaid\n"
        + mermaid_text
        + "```\n"
    )


def generate(out_dir):
    """生成依赖图并写入 out_dir，返回 (节点数, 依赖数, mmd 路径, md 路径)。

    `deps.mmd` 是纯文本，供 AI 读和版本对比；`deps.md` 是同一张图的
    Markdown 包装，供人预览，线型图例与循环依赖写在图外的正文里。
    """
    nodes = collect_nodes()
    edges = build_edges(nodes)
    mermaid_text = render_mermaid(nodes, edges)
    cycles = find_cycles(nodes, edges)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mmd_path = out_dir / "deps.mmd"
    md_path = out_dir / "deps.md"
    mmd_path.write_text(mermaid_text, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(mermaid_text, cycles), encoding="utf-8", newline="\n")
    return len(nodes), len(edges), mmd_path, md_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成模块依赖图（人 + AI 两用）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--stdout", action="store_true", help="只打印 Mermaid 文本，不写文件")
    args = parser.parse_args(argv)

    nodes = collect_nodes()
    edges = build_edges(nodes)
    mermaid_text = render_mermaid(nodes, edges)

    if args.stdout:
        sys.stdout.write(mermaid_text)
        return 0

    node_count, edge_count, mmd_path, md_path = generate(args.out_dir)
    print("已写入 {0} 和 {1}（{2} 个节点，{3} 条依赖）".format(
        mmd_path, md_path, node_count, edge_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
