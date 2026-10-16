"""review origin 消费校验 — 外围证据不得支撑非外盘类别.

被测函数只遍历 trace.candidates 的引用证据，不调用 validate_trace_against_snapshot，
因此 fixture 无需满足全链 6 阶段等约束，只要构造出合法的 Pydantic 对象即可。
"""

from datetime import UTC, datetime
from typing import Literal

import pytest

from aistock_agent.agents.workers.review import _validate_origin_consumption
from aistock_agent.schemas.market_trace import (
    CandidateExplanation,
    MarketTraceResult,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
    SourceRecord,
)
from aistock_agent.trace.chain import CausalChain, CausalNode


def _snapshot(
    origin: Literal["domestic", "peripheral"] | None = "peripheral",
    kind: Literal["market_fact", "event_evidence"] = "event_evidence",
) -> MarketTraceSnapshot:
    now = datetime.now(UTC)
    return MarketTraceSnapshot(
        snapshot_id="s1",
        trade_date="2026-10-15",
        captured_at=now,
        a_share={},
        sources={
            "EV_1": SourceRecord(
                source_id="EV_1",
                kind=kind,
                provider="jin10",
                title="美联储维持利率不变",
                content="x",
                url="https://x/1",
                occurred_at=now,
                captured_at=now,
                source_level="reporting",
                origin=origin,
            )
        },
        missing_fields=[],
        phenomenon_discovery=PhenomenonDiscoveryResult(
            status="no_phenomenon",
            primary=None,
            concurrent_phenomena=[],
            data_readiness={
                "market_data": "complete",
                "attribution_inputs": "partial",
                "causal_evidence": "not_ready",
            },
            diagnostics=[],
        ),
    )


def _trace(
    category: str,
    ev_ids: list[str],
    *,
    chain_evidence: list[str] | None = None,
) -> MarketTraceResult:
    chain = None
    if chain_evidence is not None:
        chain = CausalChain(
            nodes=[CausalNode(stage="trigger", claim="c", evidence_ids=chain_evidence)]
        )
    return MarketTraceResult(
        schema_version="1.1",
        attribution_status="hypothesis",
        candidates=[
            CandidateExplanation(
                id=category,
                category=category,  # type: ignore[arg-type]
                status="weak",
                verdict="v",
                chain=chain,
                supporting_evidence_ids=[] if chain_evidence else ev_ids,
                counter_evidence_ids=[],
            )
        ],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=[],
    )


def test_origin_consumption_blocks_wrong_category():
    with pytest.raises(ValueError):
        _validate_origin_consumption(
            _trace("industry_technology_supply", ["EV_1"]), _snapshot(origin="peripheral")
        )


def test_origin_consumption_allows_global_and_positioning():
    # global_risk_liquidity / market_positioning_liquidity 引用 peripheral → 通过（不抛）
    _validate_origin_consumption(
        _trace("global_risk_liquidity", ["EV_1"]), _snapshot(origin="peripheral")
    )
    _validate_origin_consumption(
        _trace("market_positioning_liquidity", ["EV_1"]), _snapshot(origin="peripheral")
    )


def test_origin_consumption_checks_chain_nodes():
    # 证据经 chain 节点引用也要被拦
    with pytest.raises(ValueError):
        _validate_origin_consumption(
            _trace("domestic_macro_policy", [], chain_evidence=["EV_1"]),
            _snapshot(origin="peripheral"),
        )


def test_origin_consumption_ignores_domestic_and_market_fact():
    # origin=domestic（非外围）→ 不校验
    _validate_origin_consumption(
        _trace("industry_technology_supply", ["EV_1"]), _snapshot(origin="domestic")
    )
    # kind=market_fact（非 event_evidence）→ 不校验
    _validate_origin_consumption(
        _trace("industry_technology_supply", ["EV_1"]),
        _snapshot(origin="peripheral", kind="market_fact"),
    )
