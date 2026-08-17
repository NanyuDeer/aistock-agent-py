"""导出当前所有 LangGraph 图为文件（LangGraph draw_mermaid / draw_ascii / draw_mermaid_png）。

背景：langgraph 0.2.74 中 StateGraph 未编译时无 get_graph()；编译后 get_graph() 会
构建 Pydantic input/output schema，撞上 AgentState/QuestionState 的 NotRequired
类型限制（ForbiddenQualifier）。因此本脚本仿照 CompiledGraph.get_graph()（
langgraph/graph/graph.py:535）的转换逻辑，直接读 StateGraph 内部结构
（nodes / _all_edges / branches）重建为 langchain_core.runnables.graph.Graph
（即 langgraph 官方 draw_mermaid 所依赖的可绘制对象），再调用原生绘图 API。

产出（每张图）：
  - {name}.ascii.txt    draw_ascii        —— 纯文本拓扑
  - {name}.mmd          draw_mermaid      —— mermaid 源码（可在线渲染 / CLI 转图）
  - {name}.png          draw_mermaid_png  —— 需访问 mermaid.ink，网络不通自动跳过

用法：cd aistock-agent-py && python scripts/export_langgraph_graphs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from langchain_core.runnables.graph import Graph as DrawableGraph
from langgraph.graph import END, START

from aistock_agent.graph.builder import build_graph  # noqa: E402
from aistock_agent.graph.chat_builder import build_chat_graph  # noqa: E402

OUT_DIR = ROOT / "docs" / "graphs"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def to_drawable_graph(state_graph) -> DrawableGraph:
    """StateGraph → 可绘制 Graph（仿 CompiledGraph.get_graph，跳过 schema 构建）。"""
    builder = state_graph
    g = DrawableGraph()
    start_nodes: dict[str, object] = {START: g.add_node(None, START)}
    end_nodes: dict[str, object] = {}

    def add_edge(start: str, end: str, label=None, conditional: bool = False) -> None:
        if end == END and END not in end_nodes:
            end_nodes[END] = g.add_node(None, END)
        g.add_edge(
            start_nodes[start],  # type: ignore[arg-type]
            end_nodes[end],  # type: ignore[arg-type]
            str(label) if label is not None else None,
            conditional,
        )

    for key, _n in builder.nodes.items():
        nn = g.add_node(None, key)
        start_nodes[key] = nn
        end_nodes[key] = nn

    for start, end in sorted(builder._all_edges):
        add_edge(start, end)

    for start, branches in builder.branches.items():
        default_ends = {**{k: k for k in builder.nodes if k != start}, END: END}
        for _, branch in branches.items():
            if branch.ends is not None:
                ends = branch.ends
            elif branch.then is not None:
                ends = {k: k for k in default_ends if k not in (END, branch.then)}
            else:
                ends = default_ends
            for label, end in ends.items():
                add_edge(start, end, label if label != end else None, conditional=True)
                if branch.then is not None:
                    add_edge(end, branch.then)

    for key, n in builder.nodes.items():
        if isinstance(n.ends, dict):
            for end, label in n.ends.items():
                add_edge(key, end, label, conditional=True)
        elif isinstance(n.ends, tuple):
            for end in n.ends:
                add_edge(key, end, conditional=True)

    return g


def export(name: str, state_graph) -> None:
    g = to_drawable_graph(state_graph)

    try:
        txt = g.draw_ascii()
        (OUT_DIR / f"{name}.ascii.txt").write_text(txt, encoding="utf-8")
        print(f"OK   {name}.ascii.txt")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {name}.ascii.txt: {type(e).__name__}: {e}")

    try:
        mmd = g.draw_mermaid()
        (OUT_DIR / f"{name}.mmd").write_text(mmd, encoding="utf-8")
        print(f"OK   {name}.mmd")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {name}.mmd: {type(e).__name__}: {e}")

    try:
        png = g.draw_mermaid_png()
        (OUT_DIR / f"{name}.png").write_bytes(png)
        print(f"OK   {name}.png ({len(png)} bytes)")
    except Exception as e:  # noqa: BLE001
        print(f"SKIP {name}.png（mermaid.ink 不可达或无依赖）: {type(e).__name__}: {e}")


if __name__ == "__main__":
    export("main_graph_supervisor", build_graph())
    export("chat_subgraph_qa", build_chat_graph())
    print(f"\nDONE -> {OUT_DIR}")
