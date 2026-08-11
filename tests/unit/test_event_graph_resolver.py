"""event_graph_resolver 单元测试 — Phase 1 知识图谱确定性解析

覆盖 4 条降级路径 + output_parser fail-safe 核心保留校验。
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import IndustryChainReadResult
from aistock_agent.services.event_graph_resolver import (
    normalize_industry_name,
    resolve_industry_graph_evidence,
)
from aistock_agent.utils.output_parser import transform_to_frontend


# ── 行业名称标准化 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("半导体", "半导体"),
        ("  贵金属Ⅲ  ", "贵金属"),
        ("白酒(A股)", "白酒"),
        ("电子化学品Ⅲ", "电子化学品"),
        ("消费电子零部件及组装", "消费电子零部件及组装"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_industry_name(raw: str, expected: str) -> None:
    assert normalize_industry_name(raw) == expected


# ── resolve_industry_graph_evidence：found ──────────────────────────


@pytest.mark.asyncio
async def test_resolve_found_returns_one_hop_evidence() -> None:
    """测试 1：正常 KG 返回 → status=found，含 industry/upstream/downstream。"""
    mock_data = {
        "industry": {"id": "881121.TI", "name": "半导体"},
        "upstream": [
            {
                "id": "881172.TI",
                "name": "电子化学品",
                "leadingStocks": [
                    {"code": "002409", "name": "雅克科技"},
                    {"code": "300346", "name": "南大光电"},
                ],
            }
        ],
        "downstream": [
            {
                "id": "884098.TI",
                "name": "消费电子零部件及组装",
                "leadingStocks": [
                    {"code": "689009", "name": "九号公司"},
                ],
            }
        ],
        "graphVersion": None,
        "updatedAt": "2026-07-27T08:00:00Z",
    }
    with patch(
        "aistock_agent.services.event_graph_resolver.node_api.get_industry_chain",
        new=AsyncMock(
            return_value=IndustryChainReadResult("found", mock_data, "IndustryKGService")
        ),
    ) as mock_get_chain:
        result = await resolve_industry_graph_evidence("半导体")

    assert result["status"] == "found"
    assert result["degraded"] is False
    assert result["source"] == "IndustryKGService"
    assert result["industry"]["name"] == "半导体"
    assert len(result["upstream"]) == 1
    assert result["upstream"][0]["name"] == "电子化学品"
    assert len(result["downstream"]) == 1
    assert result["missingBoundary"] is None
    mock_get_chain.assert_awaited_once_with("半导体")


# ── resolve_industry_graph_evidence：not_found ──────────────────────


@pytest.mark.asyncio
async def test_resolve_not_found_returns_degraded_but_keeps_name() -> None:
    """测试 2：行业不存在 → status=not_found, degraded=true，不抛异常。

    语义 fallback（semantic_match_industries）返回空候选时保持 degraded。
    """
    with patch(
        "aistock_agent.services.event_graph_resolver.node_api.get_industry_chain",
        new=AsyncMock(
            return_value=IndustryChainReadResult("not_found")
        ),
    ), patch(
        "aistock_agent.services.event_graph_resolver.semantic_match_industries",
        new=AsyncMock(return_value=[]),
    ):
        result = await resolve_industry_graph_evidence("不存在的行业")

    assert result["status"] == "not_found"
    assert result["degraded"] is True
    assert result["industry"] is None
    assert result["upstream"] is None
    assert result["downstream"] is None
    # 降级但确边界仍保留
    assert isinstance(result["missingBoundary"], str)
    assert len(result["missingBoundary"]) > 0


# ── resolve_industry_graph_evidence：异常 ───────────────────────────


@pytest.mark.asyncio
async def test_resolve_exception_returns_degraded() -> None:
    """测试 3：Node 接口异常 → 不抛异常，返回 degraded 证据。"""
    with patch(
        "aistock_agent.services.event_graph_resolver.node_api.get_industry_chain",
        new=AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        result = await resolve_industry_graph_evidence("半导体")

    assert result["status"] == "request_failed"
    assert result["degraded"] is True


# ── resolve_industry_graph_evidence：空名称 ─────────────────────────


@pytest.mark.asyncio
async def test_resolve_empty_name_returns_degraded() -> None:
    """空行业名 → 不请求 Node，直接返回 invalid_input。"""
    with patch(
        "aistock_agent.services.event_graph_resolver.node_api.get_industry_chain",
        new=AsyncMock(),
    ) as mock_get_chain:
        result = await resolve_industry_graph_evidence("   ")

    assert result["status"] == "invalid_input"
    assert result["degraded"] is True
    mock_get_chain.assert_not_awaited()


# ── output_parser fail-safe：KG 失败仍保留核心行业 ─────────────────


def _make_chain(core_industry: str) -> list[dict[str, object]]:
    return [
        {"industry": core_industry, "relation": "核心行业", "level": 1,
         "direction": "bullish", "impactStrength": 0.85, "reason": "影响原因"},
        {"industry": "上游行业A", "relation": "上游传导", "level": 2,
         "direction": "bullish", "impactStrength": 0.6, "reason": "传导原因"},
        {"industry": "下游行业B", "relation": "下游传导", "level": 2,
         "direction": "bullish", "impactStrength": 0.5, "reason": "传导原因"},
    ]


def test_parser_keeps_core_when_degraded_evidence() -> None:
    """测试 4：KG 失败 (degraded) → output_parser 仍保留核心行业。"""
    chain: list[object] = list(_make_chain("种植业"))
    degraded_evidence: list[dict[str, object]] = [{
        "status": "not_queried",
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": "本次未取得 IndustryKG 图谱事实",
    }]

    result = transform_to_frontend(
        understanding={"summary": "事件标题"},
        transmission={
            "mechanism": "传导机制",
            "variables": [],
            "coreIndustry": {"name": "种植业", "impact": "", "reason": ""},
            "chain": chain,
            "industryGraphEvidence": degraded_evidence,
        },
        history=None,
        investment=None,
        event_meta={"eventId": "test", "title": "t", "source": "https://a.com"},
    )

    constrained_chain = result.get("event_transmission")
    assert isinstance(constrained_chain, dict)
    chain_items = constrained_chain.get("chain")
    assert isinstance(chain_items, list)
    # 核心行业必须被保留
    assert len(chain_items) >= 1
    names = [c.get("industry") for c in chain_items if isinstance(c, dict)]
    assert "种植业" in names, f"核心行业'种植业'应在 chain 中，实际: {names}"

    # kg_unverified 标注
    core_item = next(c for c in chain_items if isinstance(c, dict) and c.get("industry") == "种植业")  # type: ignore[union-attr]
    assert core_item["kg_unverified"] is True


def test_parser_keeps_core_with_nonstandard_relation() -> None:
    """核心行业 relation 非精确'核心行业'时仍保留。"""
    chain: list[object] = [
        {"industry": "半导体", "relation": "核心", "level": 1,
         "direction": "bullish", "impactStrength": 0.9, "reason": "影响"},
    ]
    degraded_evidence: list[dict[str, object]] = [{
        "status": "invalid_response",
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": "未取得图谱事实",
    }]

    result = transform_to_frontend(
        understanding={"summary": "标题"},
        transmission={
            "mechanism": "机制",
            "variables": [],
            "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
            "chain": chain,
        },
        history=None,
        investment=None,
        event_meta={"eventId": "t", "title": "t", "source": ""},
    )

    constrained = result["event_transmission"]
    assert isinstance(constrained, dict)
    chain_items = constrained["chain"]
    assert isinstance(chain_items, list)
    assert len(chain_items) >= 1
    assert chain_items[0].get("industry") == "半导体"  # type: ignore[union-attr]


def test_parser_keeps_core_when_name_mismatch_in_found_evidence() -> None:
    """KG found 但核心行业名不匹配时 → 保留核心行业并标记 kg_unverified。"""
    chain: list[object] = [
        {"industry": "贵金属", "relation": "核心行业", "level": 1,
         "direction": "bullish", "impactStrength": 0.9, "reason": "影响"},
    ]
    # KG 返回"贵金属Ⅲ"（名称不匹配）
    found_evidence: list[dict[str, object]] = [{
        "status": "found",
        "degraded": False,
        "scope": "one_hop",
        "source": "IndustryKGService",
        "industry": {"id": "881169.TI", "name": "贵金属Ⅲ"},
        "upstream": [],
        "downstream": [],
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": None,
    }]

    # 直接测 transform_to_frontend 走 _constrain_chain_by_industry_graph
    result = transform_to_frontend(
        understanding={"summary": "金价上涨"},
        transmission={
            "mechanism": "避险情绪",
            "variables": [],
            "coreIndustry": {"name": "贵金属", "impact": "利好", "reason": "金价上涨"},
            "chain": chain,
            "industryGraphEvidence": found_evidence,
        },
        history=None,
        investment=None,
        event_meta={"eventId": "t", "title": "t", "source": ""},
    )

    constrained = result["event_transmission"]
    assert isinstance(constrained, dict)
    chain_items = constrained["chain"]
    assert isinstance(chain_items, list)
    assert len(chain_items) >= 1
    core = chain_items[0]
    assert isinstance(core, dict)
    assert core.get("industry") == "贵金属"
    assert core.get("kg_unverified") is True, (
        "名称不匹配时核心行业应标记 kg_unverified"
    )
