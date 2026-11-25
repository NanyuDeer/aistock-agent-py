"""感知节点：指数 K 线 → 3 条带溯源证据。"""

from datetime import date

import pytest

from aistock_agent.graph.brain.nodes import debate, perception, render, research, resolve
from aistock_agent.schemas.judgment import JudgmentClaim
from aistock_agent.utils.brief_contract import validate_brief_v1
from aistock_agent.utils.date import add_trading_days

ROWS = [
    {"trade_date": "2026-11-21", "close": 3180.0, "high": 3195.0, "pct_chg": -0.42},
    {"trade_date": "2026-11-24", "close": 3190.0, "high": 3205.0, "pct_chg": 0.31},
    {"trade_date": "2026-11-25", "close": 3210.0, "high": 3220.0, "pct_chg": 0.63},
]


@pytest.mark.asyncio
async def test_market_perception_builds_three_evidence(monkeypatch):
    async def _rows(code: str, days: int):
        assert code == "000001"
        assert days == 5
        return ROWS

    monkeypatch.setattr(perception, "_load_index_rows", _rows)

    update = await perception.market_perception(
        {"subject_code": "000001", "subject": "上证指数", "as_of": "2026-11-25"}
    )
    evidence = update["evidence"]
    assert [item.evidence_id for item in evidence] == [
        "ev_idx_20261125",
        "ev_idx_prev_20261124",
        "ev_idx_high_20261125",
    ]
    assert evidence[0].value == 0.63
    assert evidence[0].unit == "%"
    assert evidence[2].value == pytest.approx((3210.0 / 3220.0 - 1) * 100, abs=0.01)
    assert "home" in update["render_targets"]
    assert update["trace"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_market_perception_degrades_on_empty(monkeypatch):
    async def _rows(code: str, days: int):
        return []

    monkeypatch.setattr(perception, "_load_index_rows", _rows)

    update = await perception.market_perception({"subject_code": "000001", "as_of": "2026-11-25"})
    assert "evidence" not in update
    assert update["trace"][0]["status"] == "degraded"
    assert update["trace"][0]["error"] == "index_kline_empty"


def test_classify_primary_cause_three_branches():
    assert research.classify_primary_cause(market_pct=0.6, sector_pct=0.9, stock_pct=1.2) == "market"
    assert research.classify_primary_cause(market_pct=0.2, sector_pct=2.0, stock_pct=2.4) == "sector"
    assert research.classify_primary_cause(market_pct=-0.3, sector_pct=0.1, stock_pct=4.5) == "stock"


@pytest.mark.asyncio
async def test_insight_research_projects_claims(monkeypatch):
    async def _rows(code: str, days: int):
        return ROWS

    monkeypatch.setattr(perception, "_load_index_rows", _rows)
    perceived = await perception.market_perception(
        {"subject_code": "000001", "subject": "上证指数", "as_of": "2026-11-25"}
    )
    update = await research.insight_research(
        {"subject_code": "000001", "as_of": "2026-11-25", "evidence": perceived["evidence"]}
    )
    claims = update["claims"]
    assert len(claims) == 1
    assert claims[0].produced_by == "insight_research"
    assert claims[0].evidence_ids == ["ev_idx_20261125", "ev_idx_prev_20261124", "ev_idx_high_20261125"]
    assert update["method_prior"]["default"]["primary_cause"] == "market"


@pytest.mark.asyncio
async def test_insight_research_degrades_without_evidence():
    update = await research.insight_research({"as_of": "2026-11-25", "evidence": []})
    assert "claims" not in update
    assert update["trace"][0]["error"] == "no_market_evidence"


@pytest.mark.asyncio
async def test_bull_and_bear_use_different_evidence(monkeypatch):
    async def _rows(code: str, days: int):
        return ROWS

    monkeypatch.setattr(perception, "_load_index_rows", _rows)
    perceived = await perception.market_perception(
        {"subject_code": "000001", "subject": "上证指数", "as_of": "2026-11-25"}
    )
    researched = await research.insight_research(
        {"subject_code": "000001", "as_of": "2026-11-25", "evidence": perceived["evidence"]}
    )
    base = {
        "subject_code": "000001",
        "as_of": "2026-11-25",
        "evidence": perceived["evidence"],
        "claims": researched["claims"],
    }

    bull = await debate.bull_researcher(base)
    bear = await debate.bear_researcher({**base, "claims": [*base["claims"], *bull["claims"]]})

    bull_claim = bull["claims"][0]
    bear_claim = bear["claims"][0]
    assert bull_claim.confidence == 0.62
    assert bear_claim.confidence == 0.42
    assert bear_claim.evidence_ids == ["ev_idx_high_20261125"]
    assert set(bull_claim.evidence_ids) != set(bear_claim.evidence_ids)


@pytest.mark.asyncio
async def test_debate_degrades_without_claims():
    bull = await debate.bull_researcher({"claims": []})
    bear = await debate.bear_researcher({"claims": [], "evidence": []})
    assert bull["trace"][0]["status"] == "degraded"
    assert bear["trace"][0]["status"] == "degraded"


def _claim(produced_by: str, confidence: float, evidence_ids: list[str]) -> JudgmentClaim:
    return JudgmentClaim(
        claim_id=f"clm_{produced_by}",
        subject="上证指数",
        kind="market",
        statement="测试结论",
        evidence_ids=evidence_ids,
        confidence=confidence,
        invalidated_by=["跌破前低"],
        produced_by=produced_by,
    )


def test_detect_conflicts_only_when_gap_small():
    close_gap = [_claim("bull_researcher", 0.5, ["ev_1"]), _claim("bear_researcher", 0.46, ["ev_3"])]
    wide_gap = [_claim("bull_researcher", 0.62, ["ev_1"]), _claim("bear_researcher", 0.42, ["ev_3"])]
    assert len(resolve.detect_conflicts(close_gap)) == 1
    assert resolve.detect_conflicts(wide_gap) == []


@pytest.mark.asyncio
async def test_judgment_writer_builds_valid_judgment(monkeypatch):
    async def _rows(code: str, days: int):
        return ROWS

    monkeypatch.setattr(perception, "_load_index_rows", _rows)
    perceived = await perception.market_perception(
        {"subject_code": "000001", "subject": "上证指数", "as_of": "2026-11-25"}
    )
    researched = await research.insight_research(
        {"subject_code": "000001", "evidence": perceived["evidence"], "as_of": "2026-11-25"}
    )
    bull = await debate.bull_researcher({"claims": researched["claims"]})
    bear = await debate.bear_researcher(
        {"claims": [*researched["claims"], *bull["claims"]], "evidence": perceived["evidence"]}
    )
    conflicts = await resolve.conflict_resolver({"claims": [*researched["claims"], *bull["claims"], *bear["claims"]]})

    update = await resolve.judgment_writer(
        {
            "subject_code": "000001",
            "subject": "上证指数",
            "as_of": "2026-11-25",
            "evidence": perceived["evidence"],
            "claims": [*researched["claims"], *bull["claims"], *bear["claims"]],
            "conflicts": conflicts["conflicts"],
        }
    )
    judgment = update["judgment"]
    assert judgment.judgment_id == "jdg_20261125_000001"
    assert judgment.direction == "bullish"
    assert judgment.confidence == 0.62
    assert [item.evidence_id for item in judgment.counter_evidence] == ["ev_idx_high_20261125"]
    assert judgment.verify_at == "2026-12-07"
    assert judgment.verify_at == add_trading_days(date(2026, 12, 2), 3).isoformat()


@pytest.mark.asyncio
async def test_judgment_writer_degrades_without_debate():
    update = await resolve.judgment_writer({"as_of": "2026-11-25", "subject_code": "000001", "evidence": [], "claims": []})
    assert update["judgment"] is None
    assert update["trace"][0]["status"] == "degraded"


def _judgment_fixture():
    from aistock_agent.schemas.judgment import Judgment
    from tests.unit.test_judgment_contract import _payload

    return Judgment.model_validate(_payload())


def test_judgment_to_brief_passes_contract_and_keeps_thesis():
    judgment = _judgment_fixture()
    brief = render.judgment_to_brief(judgment)
    assert validate_brief_v1(brief) is not None
    item = brief["items"][0]
    assert item["conclusion"] == judgment.thesis
    assert item["confidence"] == "medium"
    assert "看多" in item["title"]


@pytest.mark.asyncio
async def test_briefing_writer_degrades_without_judgment():
    update = await render.briefing_writer({"as_of": "2026-11-25", "judgment": None})
    brief = update["trace"][0]["brief"]
    assert brief["degraded"] is True
    assert brief["items"] == []
    assert update["trace"][0]["status"] == "degraded"
