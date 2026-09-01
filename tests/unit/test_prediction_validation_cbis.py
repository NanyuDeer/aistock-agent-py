# tests/unit/test_prediction_validation_cbis.py
"""Spec Cbis T4：read_validation_profile 渠道B（双向印证）信号回扫。

渠道B：回扫 review 报告，从归因 trace 的 confirmed_prediction 抽取"被现实印证的
场景"信号，输出进 evidence_confirmed / scenario_harvest（T3 通道B字段）。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.target import Target
from aistock_agent.skills.prediction_validation import (
    _collect_target_confirmations,
    read_validation_profile,
)


def _review_report_with_confirmations(date: str) -> dict[str, object]:
    conf = {
        "prediction_id": "p1", "scenario": "降息预期兑现",
        "source_trace_id": "tr1", "confirmed_kind": "scene_match",
        "confirmed_at": "2026-09-01T00:00:00Z",
    }
    chain = {"nodes": [{"stage": "trigger", "claim": "x", "evidence_ids": []}], "confirmed_prediction": [conf]}
    candidate = {"id": "c1", "category": "domestic_macro_policy", "status": "supported",
                 "verdict": "v", "chain": chain, "supporting_evidence_ids": [], "counter_evidence_ids": []}
    trace = {"schema_version": "1.1", "attribution_status": "confirmed", "candidates": [candidate],
             "primary_chain_id": "c1", "alternative_chain_id": None, "confidence": "high",
             "unresolved_questions": [], "attribution_summary": "降息预期兑现驱动上行", "prediction_validation": None}
    return {"status": "completed", "content": {"market_trace": {"trace": trace, "snapshot": {
        "snapshot_id": "s1", "trade_date": date, "captured_at": "2026-09-01T00:00:00Z",
        "a_share": {}, "sources": {}, "missing_fields": [],
        "phenomenon_discovery": {"status": "detected", "primary": {"kind": "broad_rally",
            "summary": "s", "fact_ids": ["f1"], "tags": ["t"], "severity": "low"},
            "concurrent_phenomena": [], "data_readiness": {"market_data": "complete",
            "attribution_inputs": "complete", "causal_evidence": "ready"}, "diagnostics": []},
        "collection_status": {}, "data_availability": {},
    }}}}


@pytest.mark.asyncio
async def test_collect_target_confirmations_from_reports() -> None:
    target = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    with patch(
        "aistock_agent.skills.prediction_validation.node_api.list_analysis_reports",
        AsyncMock(return_value=[_review_report_with_confirmations("2026-08-31")]),
    ) as mock_list:
        got = await _collect_target_confirmations(target, window_days=1)
    assert mock_list.called
    assert any(c.get("scenario") == "降息预期兑现" for c in got)


@pytest.mark.asyncio
async def test_collect_confirmations_degrades_on_failure() -> None:
    target = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    with patch(
        "aistock_agent.skills.prediction_validation.node_api.list_analysis_reports",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        got = await _collect_target_confirmations(target, window_days=1)
    assert got == []


@pytest.mark.asyncio
async def test_read_profile_includes_channel_b() -> None:
    target = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    with patch("aistock_agent.skills.prediction_validation.node_api.list_analysis_reports",
               AsyncMock(return_value=[_review_report_with_confirmations("2026-08-31")])), \
         patch("aistock_agent.skills.prediction_validation.get_cached_validation_profile",
               AsyncMock(return_value=None)), \
         patch("aistock_agent.skills.prediction_validation.node_api.list_verified_predictions",
               AsyncMock(return_value=[])), \
         patch("aistock_agent.skills.prediction_validation.set_cached_validation_profile",
               AsyncMock(return_value=None)):
        profile = await read_validation_profile(target)
    assert profile["source"] == "rebuilt"
    assert profile.get("scenario_harvest", {}).get("confirmed", {}).get("降息预期兑现", 0) == 1
    assert "evidence_confirmed" in profile