"""P1：渠道B 回扫优先读归因结论层（root）的 confirmed_prediction。"""
from unittest.mock import AsyncMock

import pytest

from aistock_agent.services.target_profile import make_target
from aistock_agent.skills.prediction_validation import (
    _collect_target_confirmations,
    _extract_root_confirmed,
)


def test_extract_root_confirmed_reads_root_list():
    trace = {
        "confirmed_prediction": [
            {"prediction_id": "183", "scenario": "量能萎缩后回踩",
             "source_trace_id": "review:2026-10-06", "confirmed_kind": "scene_match"},
        ]
    }
    out = _extract_root_confirmed(trace)
    assert len(out) == 1 and out[0]["prediction_id"] == "183"


def test_extract_root_confirmed_tolerates_missing_or_dirty():
    assert _extract_root_confirmed({}) == []
    assert _extract_root_confirmed({"confirmed_prediction": None}) == []
    assert _extract_root_confirmed({"confirmed_prediction": ["bad"]}) == []


_CONFIRMED_AT = "2026-10-06T10:00:00Z"


def _confirmation(prediction_id: str, scenario: str) -> dict[str, object]:
    return {
        "prediction_id": prediction_id,
        "scenario": scenario,
        "source_trace_id": f"review:{prediction_id}",
        "confirmed_kind": "scene_match",
        "confirmed_at": _CONFIRMED_AT,
    }


def _minimal_trace(
    root_confirmed: list[dict[str, object]],
    primary_confirmed: list[dict[str, object]],
) -> dict[str, object]:
    """最小合法 MarketTraceResult 字典（extra=forbid，字段需齐全）。"""
    candidate = {
        "id": "c1",
        "category": "global_risk_liquidity",
        "status": "supported",
        "verdict": "v",
        "chain": {"nodes": [], "confirmed_prediction": primary_confirmed},
        "supporting_evidence_ids": [],
        "counter_evidence_ids": [],
    }
    return {
        "schema_version": "1.1",
        "attribution_status": "hypothesis",
        "candidates": [candidate],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [],
        "confirmed_prediction": root_confirmed,
    }


def _review_report(trace: dict[str, object]) -> dict[str, object]:
    return {"status": "completed", "content": {"market_trace": {"trace": trace}}}


@pytest.mark.asyncio
async def test_collect_confirmations_prefers_root_and_skips_primary(monkeypatch):
    """root 非空 → 只取 root，且**不再咨询** primary 兜底分支。"""
    target = make_target("上证指数")
    assert target is not None
    root_item = _confirmation("183", "量能萎缩后回踩")
    trace = _minimal_trace([root_item], [_confirmation("999", "不应被取到")])
    # 必须 patch 类方法：实例属性还原会在 node_api 单例上留下遮蔽类属性的实例属性（污染回放隔离）
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.list_analysis_reports",
        AsyncMock(return_value=[_review_report(trace)]),
    )
    probed: list[dict[str, object]] = []

    def _probe(trace_data: dict[str, object]) -> list[dict[str, object]]:
        probed.append(trace_data)
        return [_confirmation("999", "不应被取到")]

    monkeypatch.setattr(
        "aistock_agent.skills.prediction_validation._extract_primary_confirmed",
        _probe,
    )

    got = await _collect_target_confirmations(target, window_days=1)

    assert [c["prediction_id"] for c in got] == ["183"]
    assert probed == []


@pytest.mark.asyncio
async def test_collect_confirmations_falls_back_to_primary_when_root_empty(monkeypatch):
    """root 为空（无 confirmed_prediction）→ 回退 primary 链，且确实咨询了兜底分支。"""
    target = make_target("上证指数")
    assert target is not None
    primary_item = _confirmation("999", "降息预期兑现")
    trace = _minimal_trace([], [primary_item])
    monkeypatch.setattr(
        "aistock_agent.services.data_client.NodeApiClient.list_analysis_reports",
        AsyncMock(return_value=[_review_report(trace)]),
    )
    probed: list[dict[str, object]] = []

    def _probe(trace_data: dict[str, object]) -> list[dict[str, object]]:
        probed.append(trace_data)
        return [primary_item]

    monkeypatch.setattr(
        "aistock_agent.skills.prediction_validation._extract_primary_confirmed",
        _probe,
    )

    got = await _collect_target_confirmations(target, window_days=1)

    assert [c["prediction_id"] for c in got] == ["999"]
    assert len(probed) == 1
