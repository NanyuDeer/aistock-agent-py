"""行业知识图谱工具 — 通过 Node.js 内部接口获取概念与行业产业链事实

包含三个工具：
- ``get_concepts``：所有概念列表（数组型响应，用 ``get_list``）
- ``get_graph_by_concept``：根据概念获取产业链子图（dict 型响应，用 ``get``）
- ``get_industry_chain``：查询行业的直接上下游关系（结构化证据 JSON）
"""

import json

from langchain_core.tools import tool

from aistock_agent.services.data_client import IndustryChainReadResult, node_api
from aistock_agent.tools.base import safe_tool_call

_INDUSTRY_GRAPH_MISSING_BOUNDARY = "本次未取得 IndustryKG 图谱事实，上下游关系未展开，不能补造。"
_INDUSTRY_GRAPH_DEGRADED_STATUSES = {
    "invalid_input",
    "not_found",
    "authentication_failed",
    "upstream_failed",
    "timeout",
    "request_failed",
    "invalid_response",
}


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


@tool
@safe_tool_call
async def get_industry_chain(industry_name: str) -> str:
    """（备用工具）查询行业图谱中的直接上下游关系及行业龙头股，返回可审计 JSON 证据。

    正常情况下不调用此工具——产业链已被系统预先查询并注入 User Message
    （industryGraphEvidence）。仅在证据缺失或需人工调试其他行业时使用。

    查询结果仅表示中心行业的直接上下游事实，不包含传导方向、影响强度或原因。

    Args:
        industry_name: 由 ``match_industry_by_keywords`` 返回的规范行业名称。
    """
    normalized_name = industry_name.strip()
    if not normalized_name:
        return json.dumps(_degraded_industry_graph_evidence("invalid_input"), ensure_ascii=False)

    try:
        result = await node_api.get_industry_chain(normalized_name)
    except Exception:
        return json.dumps(_degraded_industry_graph_evidence("request_failed"), ensure_ascii=False)

    try:
        if not isinstance(result, IndustryChainReadResult):
            return json.dumps(
                _degraded_industry_graph_evidence("invalid_response"), ensure_ascii=False
            )
        return json.dumps(_to_industry_graph_evidence(result), ensure_ascii=False)
    except Exception:
        return json.dumps(
            _degraded_industry_graph_evidence("invalid_response"), ensure_ascii=False
        )


def _to_industry_graph_evidence(result: IndustryChainReadResult) -> dict[str, object]:
    """将专用客户端读取结果转换为一跳图谱证据。"""
    if result.status == "found":
        if not _is_valid_found_industry_chain_result(result):
            return _degraded_industry_graph_evidence("invalid_response")
        data = result.data
        assert data is not None
        return {
            "status": "found",
            "degraded": False,
            "scope": "one_hop",
            "source": result.source,
            "industry": data.get("industry"),
            "upstream": data.get("upstream"),
            "downstream": data.get("downstream"),
            "graphVersion": data.get("graphVersion"),
            "updatedAt": data.get("updatedAt"),
            "missingBoundary": None,
        }
    if result.status in _INDUSTRY_GRAPH_DEGRADED_STATUSES:
        return _degraded_industry_graph_evidence(result.status)
    return _degraded_industry_graph_evidence("invalid_response")


def _is_valid_found_industry_chain_result(result: IndustryChainReadResult) -> bool:
    """校验 found 响应只包含可审计的一跳 IndustryKG 事实。"""
    data = result.data
    if result.source != "IndustryKGService" or not isinstance(data, dict):
        return False
    industry = data.get("industry")
    upstream = data.get("upstream")
    downstream = data.get("downstream")
    return (
        _is_valid_industry_node(industry)
        and isinstance(upstream, list)
        and isinstance(downstream, list)
        and all(_is_valid_industry_node(node, requires_leading_stocks=True) for node in upstream)
        and all(
            _is_valid_industry_node(node, requires_leading_stocks=True)
            for node in downstream
        )
    )


def _is_valid_industry_node(value: object, *, requires_leading_stocks: bool = False) -> bool:
    """校验中心或关联行业节点的最小身份字段。"""
    if not isinstance(value, dict):
        return False
    industry_id = value.get("id")
    name = value.get("name")
    if not (
        isinstance(industry_id, str)
        and industry_id.strip()
        and isinstance(name, str)
        and name.strip()
    ):
        return False
    return not requires_leading_stocks or isinstance(value.get("leadingStocks"), list)


def _degraded_industry_graph_evidence(status: str) -> dict[str, object]:
    """构造未取得图谱事实时的统一证据边界。"""
    return {
        "status": status,
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": _INDUSTRY_GRAPH_MISSING_BOUNDARY,
    }


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
register("event", get_industry_chain, expose=False)
