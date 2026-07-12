"""行业知识图谱工具 — 通过 Node.js /internal/graph/* 获取概念与产业链子图

包含两个工具：
- ``get_concepts``：所有概念列表（数组型响应，用 ``get_list``）
- ``get_graph_by_concept``：根据概念获取产业链子图（dict 型响应，用 ``get``）
"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_concepts() -> str:
    """查询行业知识图谱所有概念列表，用于概念筛选与产业链查询入口"""
    data = await node_api.get_list("/internal/graph/concepts")
    if not data:
        return "暂无概念列表数据"
    return _format_concepts(data)


@tool
@safe_tool_call
async def get_graph_by_concept(concept: str) -> str:
    """根据概念获取产业链子图（上游/中游/下游行业 + 龙头股）

    Args:
        concept: 概念名称或概念ID，如 人工智能 或 885641.TI
    """
    data = await node_api.get(f"/internal/graph/{concept}")
    if not data:
        return f"未找到概念「{concept}」的产业链子图"
    return _format_subgraph(data, concept)


def _format_concepts(concepts: list[dict[str, object]]) -> str:
    """格式化概念列表"""
    lines: list[str] = [f"概念列表（共 {len(concepts)} 个）："]
    for concept in concepts[:30]:
        cid = concept.get("id", "-")
        name = concept.get("name", "未知")
        ind_count = concept.get("industryCount", 0)
        lines.append(f"  - {name}（{cid}）关联行业: {ind_count}")
    if len(concepts) > 30:
        lines.append(f"  ... 共 {len(concepts)} 个概念，仅显示前 30 个")
    return "\n".join(lines)


def _format_subgraph(data: dict[str, object], concept: str) -> str:
    """格式化产业链子图（KGSubGraph）"""
    center_concept = data.get("centerConcept", {})
    c_name = center_concept.get("name", concept) if isinstance(center_concept, dict) else concept

    lines: list[str] = [f"概念【{c_name}】产业链子图："]

    # 中心行业
    center_inds = data.get("centerIndustries", [])
    if isinstance(center_inds, list) and center_inds:
        lines.append("  中心行业：")
        _append_industries(lines, center_inds)

    # 上游行业
    upstream = data.get("upstreamIndustries", [])
    if isinstance(upstream, list) and upstream:
        lines.append("  上游行业：")
        _append_industries(lines, upstream)

    # 下游行业
    downstream = data.get("downstreamIndustries", [])
    if isinstance(downstream, list) and downstream:
        lines.append("  下游行业：")
        _append_industries(lines, downstream)

    return "\n".join(lines)


def _append_industries(lines: list[str], industries: list[object]) -> None:
    """将行业节点（含龙头股）追加到输出行"""
    for ind in industries:
        if not isinstance(ind, dict):
            continue
        name = ind.get("name", "未知")
        stocks = ind.get("leadingStocks", [])
        stock_str = ""
        if isinstance(stocks, list) and stocks:
            parts: list[str] = []
            for s in stocks[:3]:
                if isinstance(s, dict):
                    parts.append(f"{s.get('name', '-')}({s.get('code', '-')})")
            stock_str = "  龙头: " + ", ".join(parts) if parts else ""
        lines.append(f"    - {name}{stock_str}")


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("general", get_concepts)
register("general", get_graph_by_concept)
register("alert_graph", get_concepts)
register("alert_graph", get_graph_by_concept)
