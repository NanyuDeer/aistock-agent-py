"""事件图谱解析器 — 确定性知识图谱查询（不依赖 LLM）

职责：
    Given 核心行业名称 → 标准化 → 调用后端 IndustryKG → 生成 industryGraphEvidence。
    这是事件传导链路中“代码强制图谱查询”的统一入口，解决 LLM ReAct 跳过
    get_industry_chain 导致 not_queried 的问题（第一阶段 P0）。

输出：
    GraphEvidence 结构始终包含 coreIndustry（任何失败路径均保留），
    不抛异常——调用方不需要 try-catch，只需检查 status 字段。

使用方式（在 event.py Call2 之前调用）：
    from aistock_agent.services.event_graph_resolver import resolve_industry_graph_evidence
    evidence = await resolve_industry_graph_evidence(core_industry)
"""

import re

import structlog

from aistock_agent.services.data_client import IndustryChainReadResult, node_api
from aistock_agent.tools.industry_vector_search import semantic_match_industries

logger = structlog.get_logger()

# ── 行业名称标准化 ──────────────────────────────────────────────

# 去后缀正则（与 Node 端 buildAIEdges 中 resolveIndustryId 的模糊匹配对齐）
_CLEAN_SUFFIX_RE = re.compile(r"[ⅢⅡⅣⅠ]$|\(A股\)$")


def normalize_industry_name(name: str) -> str:
    """行业名称标准化：去后缀、去首尾空白。

    将 LLM 自由输出的“贵金属Ⅲ”规范为“贵金属”，以匹配 full_graph.json
    中存储的 canonical 名称。

    >>> normalize_industry_name("  贵金属Ⅲ  ")
    '贵金属'
    >>> normalize_industry_name("半导体")
    '半导体'
    """
    cleaned = name.strip()
    return _CLEAN_SUFFIX_RE.sub("", cleaned).strip()


# ── 证据结构 ──────────────────────────────────────────────────────

_INDUSTRY_GRAPH_MISSING_BOUNDARY = (
    "本次未取得 IndustryKG 图谱事实，上下游关系未展开，不能补造。"
)


def _degraded_evidence(status: str) -> dict[str, object]:
    """构造降级证据（内部工具，与 graph_tools.py 对齐）。"""
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


def _build_found_evidence(result: IndustryChainReadResult) -> dict[str, object]:
    """将 found 结果构造为统一证据结构。"""
    data = result.data
    assert data is not None
    return {
        "status": "found",
        "degraded": False,
        "scope": "one_hop",
        "source": "IndustryKGService",
        "industry": data.get("industry"),
        "upstream": data.get("upstream"),
        "downstream": data.get("downstream"),
        "graphVersion": data.get("graphVersion"),
        "updatedAt": data.get("updatedAt"),
        "missingBoundary": None,
    }


# ── 主入口 ──────────────────────────────────────────────────────────

# 语义 fallback 仅针对"名称未命中"（not_found）：
# 服务故障（authentication_failed/upstream_failed/timeout/request_failed/invalid_response）
# 不是名称问题，不触发 fallback，避免服务异常时叠加额外请求。
_FALLBACK_RETRY_STATUSES = frozenset({"not_found"})

# 语义 fallback 重试的候选数量上限（防过多图谱查询）
_FALLBACK_CANDIDATE_LIMIT = 3


async def _try_semantic_fallback(canonical: str) -> dict[str, object] | None:
    """语义行业匹配 fallback：用向量匹配到的规范行业名重新查询图谱。

    返回 found evidence 或 None（无候选 / 全部重试失败 / embedding 服务不可用）。
    """
    # B-2（2026-08-14）：回放模式显式短路——不发 embedding、不二次查询图谱
    # （裁决书 B 论题"embedding/语义 fallback 短路"）。此前仅靠 get_industry_chain
    # 回放降级间接短路，semantic_match_industries 的 embedding 调用未被拦截。
    from aistock_agent.iterate.replay_layer import is_replay_mode

    if is_replay_mode():
        logger.info("event_graph_resolver_semantic_fallback_skipped_replay")
        return None
    try:
        candidates = await semantic_match_industries(
            [canonical], threshold=0.7, limit=_FALLBACK_CANDIDATE_LIMIT
        )
    except Exception:
        logger.exception("event_graph_resolver_semantic_fallback_failed",
                         canonical=canonical)
        return None

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = candidate.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if name == canonical:
            continue
        try:
            retry = await node_api.get_industry_chain(name.strip())
        except Exception:
            logger.warning("event_graph_resolver_semantic_retry_failed",
                           canonical=canonical, candidate=name)
            continue
        if retry.status == "found":
            logger.info("event_graph_resolver_semantic_fallback_hit",
                        canonical=canonical, matched=name)
            return _build_found_evidence(retry)

    return None


async def resolve_industry_graph_evidence(
    core_industry: str,
) -> dict[str, object]:
    """确定性调用后端 IndustryKG 接口，返回统一 evidence。

    Step 1：精确匹配（normalize 后按名称查询）。
    Step 2：精确未命中（not_found）时，语义行业匹配 fallback——
        用向量匹配到的规范行业名重新查询图谱。
    仍失败 → degraded（调用方 fail-safe 仅保留核心行业）。

    Args:
        core_industry: LLM 输出的核心行业名（数据字段为 coreIndustry，
            此处按 Python 命名规范使用 snake_case），
            内部会标准化后再查询。

    Returns:
        统一 evidence dict，含 status / degraded / industry / upstream /
        downstream 等字段（与 graph_tools.py 输出的 JSON 结构一致）。
        任何失败路径均不抛异常。

    Raises:
        不抛异常——所有异常路径内部捕获并返回 degraded 证据。
    """
    canonical = normalize_industry_name(core_industry)
    if not canonical:
        logger.warning("event_graph_resolver_empty_canonical",
                       original=core_industry)
        return _degraded_evidence("invalid_input")

    try:
        result = await node_api.get_industry_chain(canonical)
    except Exception:
        logger.exception("event_graph_resolver_request_failed",
                         canonical=canonical)
        return _degraded_evidence("request_failed")

    if result.status == "found":
        return _build_found_evidence(result)

    # Step 2：仅名称未命中时，尝试语义行业匹配 fallback
    if result.status in _FALLBACK_RETRY_STATUSES:
        fallback_evidence = await _try_semantic_fallback(canonical)
        if fallback_evidence is not None:
            return fallback_evidence

    # 所有其他状态（not_found（fallback 失败）/ authentication_failed /
    # upstream_failed / timeout / request_failed / invalid_response）→ degraded
    logger.warning("event_graph_resolver_degraded",
                   canonical=canonical,
                   status=result.status)
    return _degraded_evidence(result.status)
