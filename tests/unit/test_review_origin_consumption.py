"""review origin 消费校验 — 外围证据不得支撑非外盘类别.

被测函数只遍历 trace.candidates 的引用证据，不调用 validate_trace_against_snapshot，
因此 fixture 无需满足全链 6 阶段等约束，只要构造出合法的 Pydantic 对象即可。
"""

from datetime import UTC, datetime
from typing import Literal

import pytest
from langchain_core.messages import SystemMessage

from aistock_agent.agents.workers.review import (
    _generate_validated_trace,
    _validate_origin_consumption,
)
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


def test_origin_consumption_blocks_domestic_macro_policy():
    # 境内政策只能由境内证据支撑，外围证据引用必拦
    with pytest.raises(ValueError):
        _validate_origin_consumption(
            _trace("domestic_macro_policy", ["EV_1"]), _snapshot(origin="peripheral")
        )


def test_origin_consumption_allows_global_positioning_industry():
    # 全球风险/流动性、市场定位、产业技术供给均可引用 peripheral（不抛）
    for category in (
        "global_risk_liquidity",
        "market_positioning_liquidity",
        "industry_technology_supply",
    ):
        _validate_origin_consumption(
            _trace(category, ["EV_1"]), _snapshot(origin="peripheral")
        )


def test_origin_consumption_checks_chain_nodes():
    # 证据经 chain 节点引用也要被拦
    with pytest.raises(ValueError):
        _validate_origin_consumption(
            _trace("domestic_macro_policy", [], chain_evidence=["EV_1"]),
            _snapshot(origin="peripheral"),
        )


@pytest.mark.asyncio
async def test_origin_violation_retries_once_with_feedback(monkeypatch):
    # 首次输出违规 → 带错误原因重试一次；第二次合规即返回，不再降级
    outputs = [
        _trace("domestic_macro_policy", ["EV_1"]).model_dump_json(),
        _trace("global_risk_liquidity", ["EV_1"]).model_dump_json(),
    ]
    seen: list[list[object]] = []

    class _FakeLLM:
        async def ainvoke(self, messages):  # noqa: ANN001
            seen.append(list(messages))
            return type("Msg", (), {"content": outputs.pop(0)})()

    monkeypatch.setattr(
        "aistock_agent.agents.workers.review.validate_trace_against_snapshot",
        lambda trace, snapshot: _validate_origin_consumption(trace, snapshot),
    )
    result = await _generate_validated_trace(
        _FakeLLM(), [SystemMessage(content="s")], _snapshot()
    )

    assert result.candidates[0].category == "global_risk_liquidity"
    assert len(seen) == 2
    assert "cannot support domestic_macro_policy" in seen[1][-1].content


@pytest.mark.asyncio
async def test_other_validation_failure_retries_then_raises(monkeypatch):
    # 任何确定性校验失败都只带反馈重试一次，二次仍失败照常抛出（由调用方降级）
    calls = 0

    class _FakeLLM:
        async def ainvoke(self, messages):  # noqa: ANN001
            nonlocal calls
            calls += 1
            return type(
                "Msg", (), {"content": _trace("global_risk_liquidity", ["EV_1"]).model_dump_json()}
            )()

    def _raise_other(trace, snapshot):  # noqa: ANN001
        raise ValueError("other semantic error")

    monkeypatch.setattr(
        "aistock_agent.agents.workers.review.validate_trace_against_snapshot",
        _raise_other,
    )
    with pytest.raises(ValueError, match="other semantic error"):
        await _generate_validated_trace(
            _FakeLLM(), [SystemMessage(content="s")], _snapshot()
        )
    assert calls == 2


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
