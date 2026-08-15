"""review 内联预测退役 + review_done 补发（PR-A/T4）单元测试。

覆盖：
- ReviewArtifact.prediction 字段保留（旧缓存 model_validate 兼容），
  但 _build_review_report 不再写 "prediction" 键（G14 源头停写）
- _persist_prediction_record 已删除（随 T4 退役，不再有独立用例）
- run() 成功路径（fresh / 缓存命中）在 _persist_review_report 后补发
  review_done（双保险，trace_id=legacy-{report_date}）
- 无默认总线：告警不发布不抛；降级路径不发布；非 scheduler/manual 不发布
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from aistock_agent.agents.workers import review as review_agent
from aistock_agent.agents.workers.review import _build_review_report, _readiness_questions
from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
    ReviewArtifact,
    SourceRecord,
)
from aistock_agent.schemas.prediction import (
    PredictionHorizon,
    PredictionResult,
    PredictionRisk,
)
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon

REPORT_DATE = "2026-08-10"
_CAPTURED_AT = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
_TRADE_DATE = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


# ============================================================================
# fixture — 无现象快照（no_phenomenon，确定性重算可复现，run() 短路不调 LLM）
# ============================================================================


def _make_source(source_id: str, **overrides) -> SourceRecord:
    defaults = {
        "source_id": source_id,
        "kind": "market_fact",
        "provider": "test",
        "title": source_id,
        "content": "test content",
        "url": None,
        "occurred_at": _TRADE_DATE,
        "captured_at": _CAPTURED_AT,
        "source_level": "market_data",
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)


_CALM_INDEXES = {
    "SH000001": {"ts_code": "000001.SH", "pct_chg": 0.1},
    "SZ399001": {"ts_code": "399001.SZ", "pct_chg": 0.1},
    "SZ399006": {"ts_code": "399006.SZ", "pct_chg": 0.1},
    "SH000300": {"ts_code": "000300.SH", "pct_chg": 0.1},
    "SH000905": {"ts_code": "000905.SH", "pct_chg": 0.1},
    "SH000852": {"ts_code": "000852.SH", "pct_chg": 0.1},
}

_CALM_SOURCES = {
    "INDEX_000001_SH": _make_source("INDEX_000001_SH", title="上证指数"),
    "INDEX_399001_SZ": _make_source("INDEX_399001_SZ"),
    "INDEX_399006_SZ": _make_source("INDEX_399006_SZ"),
    "INDEX_000300_SH": _make_source("INDEX_000300_SH"),
    "INDEX_000905_SH": _make_source("INDEX_000905_SH"),
    "INDEX_000852_SH": _make_source("INDEX_000852_SH"),
    "BREADTH_ALL": _make_source("BREADTH_ALL"),
    "TURNOVER_ALL": _make_source("TURNOVER_ALL"),
    "LIMITS_ALL": _make_source("LIMITS_ALL"),
    "MAIN_FORCE_ALL": _make_source("MAIN_FORCE_ALL"),
    "SECTORS_ALL": _make_source("SECTORS_ALL"),
}

_CALM_A_SHARE = {
    "indexes": _CALM_INDEXES,
    "breadth": {"advance_ratio": 0.5, "total_count": 5000, "decline_count": 2400},
    "turnover": {"change_pct": 1.0},
    "limits": {"up_count": 10, "down_count": 8, "broken_count": 1, "highest_board": 2},
    "main_force": {"large_and_extra_large_net_yuan": 0},
    "sectors": {
        "top_gainers": [],
        "top_losers": [],
        "top_inflows": [],
        "top_outflows": [],
    },
}


def _make_calm_snapshot() -> MarketTraceSnapshot:
    discovery = discover_market_phenomenon(_CALM_A_SHARE, _CALM_SOURCES, _CAPTURED_AT, [])
    assert discovery.status == "no_phenomenon"
    return MarketTraceSnapshot(
        snapshot_id="trace-20260810",
        trade_date=REPORT_DATE,
        captured_at=_CAPTURED_AT,
        a_share=_CALM_A_SHARE,
        sources=_CALM_SOURCES,
        missing_fields=[],
        phenomenon_discovery=discovery,
    )


def _make_calm_trace() -> MarketTraceResult:
    snapshot = _make_calm_snapshot()
    return MarketTraceResult(
        schema_version="1.1",
        attribution_status="not_applicable",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=_readiness_questions(
            snapshot.phenomenon_discovery, snapshot.missing_fields
        ),
    )


def _make_cached_artifact() -> ReviewArtifact:
    return ReviewArtifact(
        schema_version="1.1",
        snapshot=_make_calm_snapshot(),
        trace=_make_calm_trace(),
        markdown="# 缓存复盘",
        trace_summary="无",
        sectors=[],
        prediction=None,
    )


SCHEDULER_STATE = {
    "messages": [],
    "session_id": "test",
    "user_id": None,
    "favorites": [],
    "intent": None,
    "symbol": None,
    "tag_code": None,
    "analysis_reports": {},
    "final_response": None,
    "trigger_source": "scheduler",
    "report_date": REPORT_DATE,
}


def _patch_success_path(mocker):
    """mock run() 成功路径（no_phenomenon 短路，不调 LLM），返回 save mock。"""
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=None))
    mocker.patch.object(
        review_agent,
        "build_market_trace_snapshot",
        new=AsyncMock(return_value=_make_calm_snapshot()),
    )
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))
    return mocker.patch.object(
        review_agent.node_api, "save_analysis_report", new=AsyncMock()
    )


def _patch_review_done(mocker, bus):
    """mock 总线与发布器；bus=None 表示无默认总线。"""
    mocker.patch("aistock_agent.services.event_bus.get_default_bus", return_value=bus)
    return mocker.patch(
        "aistock_agent.services.event_consumers.publish_review_done",
        new=AsyncMock(),
    )


# ============================================================================
# ReviewArtifact.prediction 字段保留（旧缓存兼容，schemas 不动）
# ============================================================================


def _artifact(with_prediction: bool) -> ReviewArtifact:
    snapshot = {
        "snapshot_id": "snap-1",
        "trade_date": REPORT_DATE,
        "captured_at": "2026-08-10T00:00:00Z",
        "a_share": {"indices": [], "sectors": {}},
        "sources": {},
        "missing_fields": [],
        "phenomenon_discovery": {
            "status": "no_phenomenon",
            "primary": None,
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "complete",
                "attribution_inputs": "complete",
                "causal_evidence": "not_ready",
            },
            "diagnostics": [],
        },
    }
    trace = {
        "schema_version": "1.1",
        "attribution_status": "not_applicable",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": ["无需归因分析"],
    }
    prediction = (
        PredictionResult(
            schema_version="2.0",
            prediction_status="hypothesis",
            horizons=[
                PredictionHorizon(
                    horizon="mid",
                    remaining_estimate="2-4 周",
                    phase="peaking",
                    direction="bullish",
                    target="上证指数",
                    metric_projection="上证指数区间上移",
                    confidence="medium",
                )
            ],
            evolution_narrative="中线延续",
            risks=[PredictionRisk(factor="政策转向", invalidation="宽松转紧失效")],
            evidence_ids=[],
        )
        if with_prediction
        else None
    )
    return ReviewArtifact(
        schema_version="1.1",
        snapshot=snapshot,
        trace=trace,
        markdown="# A股收盘溯源",
        trace_summary="无",
        sectors=[],
        prediction=prediction,
    )


def test_artifact_prediction_roundtrip():
    artifact = _artifact(with_prediction=True)
    dumped = artifact.model_dump(mode="json")
    restored = ReviewArtifact.model_validate(dumped)
    assert restored.prediction is not None
    assert restored.prediction.horizons[0].horizon == "mid"


def test_artifact_prediction_defaults_none():
    artifact = _artifact(with_prediction=False)
    assert artifact.prediction is None


# ============================================================================
# _build_review_report 停写 prediction（G14 源头，前端已改读 records）
# ============================================================================


def test_build_review_report_excludes_prediction():
    """market_trace 不再包含 "prediction" 键，即使 artifact.prediction 非 None。"""
    content = _build_review_report(_artifact(with_prediction=True))
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    assert "prediction" not in market_trace
    assert "snapshot" in market_trace
    assert "trace" in market_trace


# ============================================================================
# run() 成功持久化后补发 review_done（双保险，硬约束 1）
# ============================================================================


@pytest.mark.asyncio
async def test_run_publishes_review_done_on_scheduler_success(mocker):
    """fresh 路径成功持久化（scheduler）→ 补发 review_done（幂等 legacy trace_id）。"""
    _patch_success_path(mocker)
    fake_bus = object()
    publish = _patch_review_done(mocker, fake_bus)

    result = await review_agent.run(dict(SCHEDULER_STATE))

    assert result["final_response"] != review_agent.DEGRADED_RESPONSE
    publish.assert_awaited_once_with(
        fake_bus, report_date=REPORT_DATE, trace_id=f"legacy-{REPORT_DATE}"
    )


@pytest.mark.asyncio
async def test_run_cache_hit_publishes_review_done(mocker):
    """缓存命中路径成功持久化（scheduler）→ 同样补发 review_done。"""
    cached = _make_cached_artifact().model_dump(mode="json")
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=cached))
    mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))
    mocker.patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock())
    fake_bus = object()
    publish = _patch_review_done(mocker, fake_bus)

    result = await review_agent.run(dict(SCHEDULER_STATE))

    assert result["final_response"] != review_agent.DEGRADED_RESPONSE
    publish.assert_awaited_once_with(
        fake_bus, report_date=REPORT_DATE, trace_id=f"legacy-{REPORT_DATE}"
    )


@pytest.mark.asyncio
async def test_run_no_event_bus_warns_and_skips_publish(mocker):
    """无默认总线：告警不发布不抛，review 正常返回（旧串行断链显式可观测）。"""
    _patch_success_path(mocker)
    publish = _patch_review_done(mocker, None)

    result = await review_agent.run(dict(SCHEDULER_STATE))

    assert result["final_response"] != review_agent.DEGRADED_RESPONSE
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_degraded_does_not_publish_review_done(mocker):
    """降级路径（快照构建失败）不发布 review_done。"""
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=None))
    mocker.patch.object(
        review_agent,
        "build_market_trace_snapshot",
        new=AsyncMock(side_effect=RuntimeError("snapshot unavailable")),
    )
    publish = _patch_review_done(mocker, object())

    result = await review_agent.run(dict(SCHEDULER_STATE))

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_without_trigger_source_does_not_publish(mocker):
    """非 scheduler/manual 触发（如普通用户访问）：不持久化也不发布 review_done。"""
    save = _patch_success_path(mocker)
    publish = _patch_review_done(mocker, object())
    state = {k: v for k, v in SCHEDULER_STATE.items() if k != "trigger_source"}

    result = await review_agent.run(state)

    assert result["final_response"] != review_agent.DEGRADED_RESPONSE
    save.assert_not_awaited()
    publish.assert_not_awaited()
