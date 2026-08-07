"""新增 schema 模型的序列化/反序列化测试。"""
from datetime import UTC, datetime

from aistock_agent.schemas.market_trace import (
    DataReadiness,
    EventHit,
    MarketTraceResult,
    MarketTraceSnapshot,
    MorningEvent,
    MorningForecast,
    MorningSectorView,
    PhenomenonDiscoveryResult,
    PredictionValidation,
    SectorHit,
)


def test_morning_forecast_roundtrip():
    forecast = MorningForecast(
        report_date="2026-08-02",
        summary="A股有望震荡茁壮上行",
        major_events=[
            MorningEvent(title="美联储维持利率", direction="bullish", affected_sectors=["券商"]),
        ],
        sectors=[
            MorningSectorView(sector="券商", direction="bullish", note="政策利好"),
        ],
        risks=["外部地缘风险"],
        source_report_id="rpt_001",
    )
    raw = forecast.model_dump_json()
    restored = MorningForecast.model_validate_json(raw)
    assert restored == forecast


def test_prediction_validation_roundtrip():
    pv = PredictionValidation(
        status="partial",
        sector_hits=[
            SectorHit(
                sector="券商",
                morning_direction="bullish",
                actual_direction="bearish",
                result="miss",
                deviation_note="政策利好未兑现",
            ),
        ],
        event_hits=[
            EventHit(
                event_title="美联储维持利率",
                morning_direction="bullish",
                actual_impact="市场反应平淡",
                result="miss",
                note="已 price-in",
            ),
        ],
        overall_note="板块方向部分偏离",
    )
    raw = pv.model_dump_json()
    restored = PredictionValidation.model_validate_json(raw)
    assert restored == pv


def test_market_trace_result_prediction_validation_optional_default_none():
    """prediction_validation 默认 None，兼容旧缓存。"""
    result = MarketTraceResult(
        schema_version="1.1",
        attribution_status="hypothesis",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=[],
    )
    assert result.prediction_validation is None


def test_market_trace_result_with_prediction_validation():
    """带 prediction_validation 的 MarketTraceResult 可序列化。"""
    pv = PredictionValidation(
        status="hit",
        sector_hits=[],
        event_hits=[],
        overall_note="全部命中",
    )
    result = MarketTraceResult(
        schema_version="1.1",
        attribution_status="confirmed",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="high",
        unresolved_questions=[],
        prediction_validation=pv,
    )
    raw = result.model_dump_json()
    restored = MarketTraceResult.model_validate_json(raw)
    assert restored.prediction_validation is not None
    assert restored.prediction_validation.status == "hit"


def test_market_trace_result_attribution_summary_optional_default_none():
    """attribution_summary 默认 None，兼容旧缓存。"""
    result = MarketTraceResult(
        schema_version="1.1",
        attribution_status="insufficient",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=[],
    )
    assert result.attribution_summary is None


def test_market_trace_result_attribution_summary_roundtrip():
    """带 attribution_summary 的 MarketTraceResult 可序列化。"""
    result = MarketTraceResult(
        schema_version="1.1",
        attribution_status="confirmed",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="high",
        unresolved_questions=[],
        attribution_summary="AI算力与创新药业绩驱动CRO/PCB板块领涨",
    )
    raw = result.model_dump_json()
    restored = MarketTraceResult.model_validate_json(raw)
    assert restored.attribution_summary == "AI算力与创新药业绩驱动CRO/PCB板块领涨"


def test_market_trace_snapshot_morning_forecast_optional_default_none():
    """snapshot.morning_forecast 默认 None，兼容旧缓存。"""
    # 用最小可用 snapshot（其他必填字段用占位），复用 test_market_trace_snapshot.py 的最小字段模式
    snapshot = MarketTraceSnapshot(
        snapshot_id="trace-min",
        trade_date="2026-08-02",
        captured_at=datetime(2026, 8, 2, tzinfo=UTC),
        a_share={},
        sources={},
        missing_fields=[],
        phenomenon_discovery=PhenomenonDiscoveryResult(
            status="no_phenomenon",
            primary=None,
            concurrent_phenomena=[],
            data_readiness=DataReadiness(
                market_data="complete",
                attribution_inputs="missing",
                causal_evidence="not_ready",
            ),
            diagnostics=[],
        ),
    )
    assert snapshot.morning_forecast is None
