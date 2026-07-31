"""evidence_resolver Skill 单元测试。

覆盖：
1. Redis 工件有效 → 非 degraded，不调用 load_validated_trace
2. Redis 工件损坏 → 忽略缓存，调用 load_validated_trace
3. Redis 日期不一致 → 忽略缓存，调用 load_validated_trace
4. Redis 跨对象校验失败 → 忽略缓存，调用 load_validated_trace
5. 两处都无有效工件 → facts/source 为空的 degraded
6. Redis 或 Node helper 异常 → @skill 返回 degraded
7. 不调用 review.run、run_review、build_market_trace_snapshot、
   build_quick_snapshot、Tavily、LLM
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.schemas.market_trace import (
    DataReadiness,
    MarketTraceResult,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
)
from aistock_agent.skills.evidence_resolver import evidence_resolver, resolve_trace_evidence


def _goal() -> InsightGoal:
    # 使用已有 Literal；evidence_resolver 不依赖 goal.intent 值
    return InsightGoal(question="今天市场证据", intent="trace_lookup")


# ============================================================================
# 共享测试数据
# ============================================================================

# Redis 缓存中有效的 ReviewArtifact dict（insufficient_data 路径，
# 可自然通过 ReviewArtifact.model_validate + validate_trace_against_snapshot）
VALID_ARTIFACT_DICT: dict = {
    "schema_version": "1.1",
    "snapshot": {
        "snapshot_id": "trace-20260728",
        "trade_date": "2026-07-28",
        "captured_at": "2026-07-28T15:00:00+00:00",
        "a_share": {},
        "sources": {},
        "missing_fields": [],
        "phenomenon_discovery": {
            "status": "insufficient_data",
            "primary": None,
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "incomplete",
                "attribution_inputs": "missing",
                "causal_evidence": "not_ready",
            },
            "diagnostics": [],
        },
    },
    "trace": {
        "schema_version": "1.1",
        "attribution_status": "insufficient",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [
            "市场数据不足以支撑归因分析",
            "因果证据充分性不足，依赖 partial 或 not_ready 来源",
        ],
    },
    "markdown": "# A股收盘溯源\n快照编号：trace-20260728",
    "trace_summary": "测试摘要",
    "sectors": [],
}

# Node load_validated_trace 返回的有效 (snapshot, trace) 元组
VALID_NODE_SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260728",
    trade_date="2026-07-28",
    captured_at=datetime(2026, 7, 28, 15, 0, tzinfo=UTC),
    a_share={},
    sources={},
    missing_fields=[],
    phenomenon_discovery=PhenomenonDiscoveryResult(
        status="insufficient_data",
        primary=None,
        concurrent_phenomena=[],
        data_readiness=DataReadiness(
            market_data="incomplete",
            attribution_inputs="missing",
            causal_evidence="not_ready",
        ),
        diagnostics=[],
    ),
)

VALID_NODE_TRACE = MarketTraceResult(
    schema_version="1.1",
    attribution_status="insufficient",
    candidates=[],
    primary_chain_id=None,
    alternative_chain_id=None,
    confidence="low",
    unresolved_questions=[
        "市场数据不足以支撑归因分析",
        "因果证据充分性不足，依赖 partial 或 not_ready 来源",
    ],
)


# ============================================================================
# Test 1: Redis 命中 → 有效工件
# ============================================================================


@pytest.mark.asyncio
async def test_redis_hit_returns_valid_evidence():
    """Redis 工件有效 → 非 degraded，不调用 load_validated_trace。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value=VALID_ARTIFACT_DICT),
    ), patch(
        "aistock_agent.skills.evidence_resolver.validate_trace_against_snapshot",
    ) as mock_validate, patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(),
    ) as mock_node:
        ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

    assert ev.degraded is False
    assert ev.skill_name == "evidence_resolver"
    assert any("交易日: 2026-07-28" in f for f in ev.facts)
    assert any("现象状态: insufficient_data" in f for f in ev.facts)
    assert any("归因状态: insufficient" in f for f in ev.facts)
    assert any(s.kind == "trace" for s in ev.sources)
    assert ev.raw.get("origin") == "redis"
    assert ev.raw.get("snapshot_id") == "trace-20260728"
    mock_validate.assert_called_once()
    mock_node.assert_not_called()


# ============================================================================
# Test 2: Redis 工件损坏 → fallback 到 Node
# ============================================================================


@pytest.mark.asyncio
async def test_redis_broken_artifact_falls_to_node():
    """Redis 返回无法解析的 dict → 忽略缓存，调用 load_validated_trace。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value={"bad": "data"}),
    ), patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(return_value=(VALID_NODE_SNAPSHOT, VALID_NODE_TRACE)),
    ) as mock_node:
        ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

    assert ev.degraded is False
    assert ev.raw.get("origin") == "node"
    mock_node.assert_awaited_once_with("2026-07-28")


# ============================================================================
# Test 3: Redis 日期不一致 → fallback 到 Node
# ============================================================================


@pytest.mark.asyncio
async def test_redis_date_mismatch_falls_to_node():
    """Redis 缓存 trade_date 与请求日期不一致 → 忽略缓存，调 Node。"""
    artifact = dict(VALID_ARTIFACT_DICT)
    artifact["snapshot"] = dict(artifact["snapshot"])
    artifact["snapshot"]["trade_date"] = "2026-07-27"  # 与请求日期不一致

    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value=artifact),
    ), patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(return_value=(VALID_NODE_SNAPSHOT, VALID_NODE_TRACE)),
    ) as mock_node:
        ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

    assert ev.degraded is False
    assert ev.raw.get("origin") == "node"
    mock_node.assert_awaited_once_with("2026-07-28")


# ============================================================================
# Test 4: Redis 跨对象校验失败 → fallback 到 Node
# ============================================================================


@pytest.mark.asyncio
async def test_redis_validation_fail_falls_to_node():
    """Redis 工件 model_validate 通过但 validate_trace_against_snapshot 失败。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value=VALID_ARTIFACT_DICT),
    ), patch(
        "aistock_agent.skills.evidence_resolver.validate_trace_against_snapshot",
        side_effect=ValueError("simulated validation failure"),
    ), patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(return_value=(VALID_NODE_SNAPSHOT, VALID_NODE_TRACE)),
    ) as mock_node:
        ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

    assert ev.degraded is False
    assert ev.raw.get("origin") == "node"
    mock_node.assert_awaited_once_with("2026-07-28")


# ============================================================================
# Test 5: 两处都无有效工件 → degraded
# ============================================================================


@pytest.mark.asyncio
async def test_both_miss_returns_degraded():
    """Redis 与 Node 均无有效工件 → facts/sources 为空的 degraded。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(return_value=None),
    ):
        ev = await evidence_resolver({"date": "1999-01-01"}, _goal())

    assert ev.degraded is True
    assert ev.skill_name == "evidence_resolver"
    assert ev.facts == []
    assert ev.sources == []
    assert ev.raw == {}


# ============================================================================
# Test 6: 异常 → @skill 降级
# ============================================================================


@pytest.mark.asyncio
async def test_redis_exception_degraded():
    """Redis 异常 → @skill 返回 degraded evidence。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(side_effect=RuntimeError("redis down")),
    ):
        ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

    assert ev.degraded is True
    assert "evidence_resolver" in (ev.degraded_reason or "")
    assert ev.skill_name == "evidence_resolver"


@pytest.mark.asyncio
async def test_node_exception_degraded():
    """Node helper（load_validated_trace）异常 → @skill 返回 degraded evidence。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(side_effect=RuntimeError("node down")),
    ):
        ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

    assert ev.degraded is True
    assert "evidence_resolver" in (ev.degraded_reason or "")
    assert ev.skill_name == "evidence_resolver"


# ============================================================================
# Test 7: resolve_trace_evidence 直接调用
# ============================================================================


@pytest.mark.asyncio
async def test_resolve_trace_evidence_node_path():
    """resolve_trace_evidence helper 走 Node 路径返回非 degraded。"""
    with patch(
        "aistock_agent.skills.evidence_resolver.get_cached_review",
        new=AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(return_value=(VALID_NODE_SNAPSHOT, VALID_NODE_TRACE)),
    ):
        ev = await resolve_trace_evidence("2026-07-28", skill_name="test_skill")

    assert ev.degraded is False
    assert ev.skill_name == "test_skill"
    assert ev.raw.get("origin") == "node"
    assert any(s.kind == "trace" for s in ev.sources)


# ============================================================================
# Test 8: 不调用 forbidden 函数
# ============================================================================


@pytest.mark.asyncio
async def test_forbidden_functions_not_called():
    """证据解析路径不调用 review、快照构建、Tavily、LLM。"""
    mock_run = MagicMock()
    mock_run_review = MagicMock()
    mock_build_trace = MagicMock()
    mock_build_quick = MagicMock()
    mock_llm = MagicMock()
    mock_tavily = MagicMock()

    patches = [
        patch(
            "aistock_agent.agents.workers.review.run",
            mock_run,
        ),
        patch(
            "aistock_agent.agents.workers.review.run_review",
            mock_run_review,
        ),
        patch(
            "aistock_agent.services.market_trace_snapshot.build_market_trace_snapshot",
            mock_build_trace,
        ),
        patch(
            "aistock_agent.services.market_trace_snapshot.build_quick_snapshot",
            mock_build_quick,
        ),
        patch(
            "aistock_agent.services.llm.get_deep_think",
            mock_llm,
        ),
        patch(
            "aistock_agent.services.tavily.TavilyService.search",
            mock_tavily,
        ),
    ]
    for p in patches:
        p.start()

    try:
        with patch(
            "aistock_agent.skills.evidence_resolver.get_cached_review",
            new=AsyncMock(return_value=None),
        ), patch(
            "aistock_agent.skills.evidence_resolver.load_validated_trace",
            new=AsyncMock(return_value=None),
        ):
            ev = await evidence_resolver({"date": "2026-07-28"}, _goal())

        assert ev.degraded is True
        mock_run.assert_not_called()
        mock_run_review.assert_not_called()
        mock_build_trace.assert_not_called()
        mock_build_quick.assert_not_called()
        mock_llm.assert_not_called()
        mock_tavily.assert_not_called()
    finally:
        for p in patches:
            p.stop()
