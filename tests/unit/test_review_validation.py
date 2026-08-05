"""validate_trace_against_snapshot 跨对象校验单元测试

覆盖 Task 8 新增的 prediction_validation 校验分支。

fixture 参照 tests/integration/test_review_agent.py 的 TRACE_SNAPSHOT/VALID_TRACE_DICT
构造模式：phenomenon_discovery 必须经 discover_market_phenomenon 重算得出（而非硬编码），
a_share 必须含完整 indexes/breadth/turnover/limits/main_force/sectors 字段，
sources 必须含全部 market_fact（INDEX_*/BREADTH_ALL/TURNOVER_ALL/LIMITS_ALL/MAIN_FORCE_ALL/SECTORS_ALL）
+ event_evidence（NEWS_001/SEARCH_001）+ yfinance（GLOBAL_001），
确保能通过 validate_snapshot_discovery 重算校验后到达 prediction_validation 校验分支。
"""

import copy
from datetime import UTC, datetime

import pytest

from aistock_agent.agents.workers.review import (
    validate_snapshot_discovery,
    validate_trace_against_snapshot,
)
from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
    MorningForecast,
    PredictionValidation,
    SectorHit,
    SourceRecord,
)
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon

# ============================================================================
# fixture 辅助 — 参照 test_review_agent.py 的 _A_SHARE/_SOURCES/TRACE_SNAPSHOT
# ============================================================================

_CAPTURED_AT = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)
_TRADE_DATE = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _make_source(source_id: str, **overrides: object) -> SourceRecord:
    defaults: dict[str, object] = {
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
    return SourceRecord(**defaults)  # type: ignore[arg-type]


_A_SHARE: dict[str, object] = {
    "indexes": {
        "SH000001": {"ts_code": "000001.SH", "pct_chg": 1.2},
        "SZ399001": {"ts_code": "399001.SZ", "pct_chg": 1.5},
        "SZ399006": {"ts_code": "399006.SZ", "pct_chg": 1.8},
        "SH000300": {"ts_code": "000300.SH", "pct_chg": 1.0},
        "SH000905": {"ts_code": "000905.SH", "pct_chg": 0.9},
        "SH000852": {"ts_code": "000852.SH", "pct_chg": 1.1},
    },
    "breadth": {"advance_ratio": 0.75, "total_count": 5000, "decline_count": 1000},
    "turnover": {"change_pct": 15.0},
    "limits": {"up_count": 50, "down_count": 10, "broken_count": 5, "highest_board": 3},
    "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
    "sectors": {
        "top_gainers": [{"name": "半导体"}],
        "top_losers": [{"name": "房地产"}],
        "top_inflows": [],
        "top_outflows": [],
    },
}


def _make_sources() -> dict[str, SourceRecord]:
    """构建能通过 validate_snapshot_discovery 的来源集合。

    含 6 个 INDEX_* market_fact（满足 _index_returns 的 6+ 索引要求）、
    BREADTH_ALL/TURNOVER_ALL/LIMITS_ALL/MAIN_FORCE_ALL/SECTORS_ALL market_fact、
    NEWS_001/SEARCH_001 event_evidence（含 url + occurred_at <= captured_at，满足 ready 因果证据）、
    GLOBAL_001（yfinance market_fact，备选链用）。
    """
    return {
        "INDEX_000001_SH": _make_source(
            "INDEX_000001_SH",
            provider="tushare:index_daily",
            title="上证指数",
            content="close=3200.0, pct_chg=0.5",
        ),
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
        "GLOBAL_001": _make_source(
            "GLOBAL_001",
            provider="yfinance",
            title="标普500",
            content="price=5500.0, change_pct=0.36",
        ),
        "NEWS_001": _make_source(
            "NEWS_001",
            kind="event_evidence",
            provider="cls",
            title="央行宣布降准",
            content="中国人民银行决定下调存款准备金率0.5个百分点",
            url="https://www.cls.cn/news/1",
            source_level="reporting",
        ),
        "SEARCH_001": _make_source(
            "SEARCH_001",
            kind="event_evidence",
            provider="tavily",
            title="美联储维持利率不变",
            content="美联储在最新议息会议上决定维持联邦基金利率目标区间不变",
            url="https://example.com/fed",
            source_level="reporting",
        ),
    }


def _make_valid_snapshot(
    morning_forecast: MorningForecast | None = None,
) -> MarketTraceSnapshot:
    """构造能通过 validate_snapshot_discovery 的 detected 快照。

    morning_forecast 默认 None（旧缓存兼容路径）。
    phenomenon_discovery 由 discover_market_phenomenon 重算得出（broad_rally detected），
    discovery.status="detected" 避开 deterministic empty trace 早退分支。
    """
    sources = _make_sources()
    return MarketTraceSnapshot(
        snapshot_id="trace-20260717",
        trade_date="2026-07-17",
        captured_at=_CAPTURED_AT,
        a_share=_A_SHARE,
        sources=sources,
        missing_fields=[],
        phenomenon_discovery=discover_market_phenomenon(_A_SHARE, sources, _CAPTURED_AT, []),
        morning_forecast=morning_forecast,
    )


# ============================================================================
# SNAPSHOT fixture — 从已退役的 QA 服务测试迁移（validate_snapshot_discovery 用例依赖）
# ============================================================================

_REPORT_DATE = "2026-07-17"

_SOURCES: dict[str, SourceRecord] = {
    "INDEX_000001_SH": _make_source(
        "INDEX_000001_SH",
        provider="tushare:index_daily",
        title="上证指数",
        content="close=3200.0, pct_chg=0.5",
    ),
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
    "NEWS_001": _make_source(
        "NEWS_001",
        kind="event_evidence",
        provider="cls",
        title="央行宣布降准",
        content="中国人民银行决定下调存款准备金率0.5个百分点",
        url="https://www.cls.cn/news/1",
        source_level="reporting",
    ),
    "GLOBAL_001": _make_source(
        "GLOBAL_001",
        provider="yfinance",
        title="标普500",
        content="price=5500.0, change_pct=0.36",
    ),
    "SEARCH_001": _make_source(
        "SEARCH_001",
        kind="event_evidence",
        provider="tavily",
        title="美联储维持利率不变",
        content="美联储在最新议息会议上决定维持联邦基金利率目标区间不变",
        url="https://example.com/fed",
        source_level="reporting",
    ),
}

SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260717",
    trade_date=_REPORT_DATE,
    captured_at=_CAPTURED_AT,
    a_share=_A_SHARE,
    sources=_SOURCES,
    missing_fields=[],
    phenomenon_discovery=discover_market_phenomenon(_A_SHARE, _SOURCES, _CAPTURED_AT, []),
)


_VALID_TRACE_DICT: dict[str, object] = {
    "schema_version": "1.1",
    "attribution_status": "confirmed",
    "candidates": [
        {
            "id": "global_risk_liquidity",
            "category": "global_risk_liquidity",
            "status": "weak",
            "verdict": "全球风险偏好改善但非主因",
            "chain": {
                "nodes": [
                    {
                        "stage": "structural_root",
                        "claim": "美联储维持利率",
                        "evidence_ids": ["SEARCH_001"],
                    },
                    {
                        "stage": "trigger",
                        "claim": "全球流动性宽松预期",
                        "evidence_ids": ["GLOBAL_001"],
                    },
                    {
                        "stage": "transmission",
                        "claim": "外资流入新兴市场",
                        "evidence_ids": ["GLOBAL_001"],
                    },
                    {
                        "stage": "exposure",
                        "claim": "北向资金净流入",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "repricing",
                        "claim": "权重股估值抬升",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "observable_result",
                        "claim": "上证指数上涨0.5%",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                ]
            },
            "supporting_evidence_ids": ["GLOBAL_001", "SEARCH_001"],
            "counter_evidence_ids": [],
        },
        {
            "id": "domestic_macro_policy",
            "category": "domestic_macro_policy",
            "status": "supported",
            "verdict": "央行降准释放流动性是主因",
            "chain": {
                "nodes": [
                    {
                        "stage": "structural_root",
                        "claim": "国内货币政策宽松周期",
                        "evidence_ids": ["NEWS_001"],
                    },
                    {
                        "stage": "trigger",
                        "claim": "央行宣布降准0.5个百分点",
                        "evidence_ids": ["NEWS_001"],
                    },
                    {
                        "stage": "transmission",
                        "claim": "银行间流动性宽松传导至权益",
                        "evidence_ids": ["NEWS_001"],
                    },
                    {
                        "stage": "exposure",
                        "claim": "金融板块直接受益",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "repricing",
                        "claim": "市场情绪回暖",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "observable_result",
                        "claim": "上证指数上涨0.5%",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                ]
            },
            "supporting_evidence_ids": ["NEWS_001", "INDEX_000001_SH"],
            "counter_evidence_ids": [],
        },
        {
            "id": "industry_technology_supply",
            "category": "industry_technology_supply",
            "status": "insufficient",
            "verdict": "无明确产业供给冲击",
            "chain": None,
            "supporting_evidence_ids": [],
            "counter_evidence_ids": [],
        },
        {
            "id": "market_positioning_liquidity",
            "category": "market_positioning_liquidity",
            "status": "rejected",
            "verdict": "市场定位与流动性非独立驱动因素",
            "chain": None,
            "supporting_evidence_ids": [],
            "counter_evidence_ids": ["INDEX_000001_SH"],
        },
    ],
    "primary_chain_id": "domestic_macro_policy",
    "alternative_chain_id": "global_risk_liquidity",
    "confidence": "high",
    "unresolved_questions": ["降准对银行净息差的长期影响尚不明确"],
}


def _make_valid_trace(
    prediction_validation: PredictionValidation | None = None,
) -> MarketTraceResult:
    """构造能通过现有校验的 confirmed trace。

    prediction_validation 默认 None（旧缓存兼容路径）。
    primary=supported（domestic_macro_policy），alternative=weak（global_risk_liquidity），
    primary trigger 引用 NEWS_001（event_evidence + url + occurred_at <= captured_at），
    observable_result 引用 INDEX_000001_SH（primary phenomenon fact_id + market_fact）。
    """
    trace_dict = copy.deepcopy(_VALID_TRACE_DICT)
    if prediction_validation is not None:
        trace_dict["prediction_validation"] = prediction_validation.model_dump(mode="json")
    return MarketTraceResult.model_validate(trace_dict)


def _make_morning_forecast() -> MorningForecast:
    """构造非空 morning_forecast，用于触发 prediction_validation 强制校验。"""
    return MorningForecast(
        report_date="2026-07-17",
        summary="早盘预测：央行降准利好，关注金融板块",
        major_events=[],
        sectors=[],
        risks=[],
        source_report_id=None,
    )


# ============================================================================
# prediction_validation 校验测试（Task 8）
# ============================================================================


def test_validate_prediction_validation_no_forecast_when_morning_absent():
    """morning_forecast 为空时，prediction_validation 必须为 None 或 status=no_forecast。

    本测试：morning_forecast=None + prediction_validation=None → 校验通过。
    """
    snapshot = _make_valid_snapshot(morning_forecast=None)
    trace = _make_valid_trace(prediction_validation=None)

    # 期望不抛 ValueError
    validate_trace_against_snapshot(trace, snapshot)


def test_validate_prediction_validation_required_when_morning_present():
    """morning_forecast 非空时，prediction_validation 不得为 None。"""
    snapshot = _make_valid_snapshot(morning_forecast=_make_morning_forecast())
    trace = _make_valid_trace(prediction_validation=None)

    with pytest.raises(ValueError, match="prediction_validation 不得为 None"):
        validate_trace_against_snapshot(trace, snapshot)


def test_validate_prediction_validation_no_forecast_empty_hits():
    """status=no_forecast 时 sector_hits/event_hits 必须为空。

    morning_forecast=None + pv.status=no_forecast + pv.sector_hits=[非空] → ValueError。
    """
    snapshot = _make_valid_snapshot(morning_forecast=None)
    pv = PredictionValidation(
        status="no_forecast",
        sector_hits=[
            SectorHit(
                sector="半导体",
                morning_direction="bullish",
                actual_direction="bullish",
                result="hit",
                deviation_note="",
            )
        ],
        event_hits=[],
        overall_note="",
    )
    trace = _make_valid_trace(prediction_validation=pv)

    with pytest.raises(
        ValueError,
        match="sector_hits/event_hits 必须为空",
    ):
        validate_trace_against_snapshot(trace, snapshot)


def test_validate_prediction_validation_partial_non_empty_sector_hits():
    """status=hit/partial/miss 时 sector_hits 不得为空。

    morning_forecast 非空 + pv.status="hit" + pv.sector_hits=[] → ValueError。
    """
    snapshot = _make_valid_snapshot(morning_forecast=_make_morning_forecast())
    pv = PredictionValidation(
        status="hit",
        sector_hits=[],
        event_hits=[],
        overall_note="",
    )
    trace = _make_valid_trace(prediction_validation=pv)

    with pytest.raises(
        ValueError,
        match="sector_hits 不得为空",
    ):
        validate_trace_against_snapshot(trace, snapshot)


def test_validate_prediction_validation_none_passes_for_old_cache():
    """旧缓存兼容：prediction_validation=None 时校验通过（无 morning_forecast）。

    强调旧格式缓存没有 prediction_validation 字段（Pydantic 默认 None），
    且 snapshot 无 morning_forecast，校验应通过以保持向后兼容。
    """
    snapshot = _make_valid_snapshot(morning_forecast=None)
    # 直接构造不含 prediction_validation 字段的 dict，模拟旧缓存
    trace_dict = copy.deepcopy(_VALID_TRACE_DICT)
    assert "prediction_validation" not in trace_dict
    trace = MarketTraceResult.model_validate(trace_dict)
    assert trace.prediction_validation is None

    # 期望不抛 ValueError
    validate_trace_against_snapshot(trace, snapshot)


# ============================================================================
# validate_snapshot_discovery 校验测试（从已退役的 QA 服务测试迁移）
# ============================================================================


def test_validation_accepts_jsonb_reordered_sources() -> None:
    """JSONB source-map insertion order must not change discovery validity."""
    reordered = dict(reversed(list(SNAPSHOT.sources.items())))
    persisted = SNAPSHOT.model_copy(update={"sources": reordered})

    validate_snapshot_discovery(persisted)


def test_validation_rejects_duplicate_diagnostic_evidence_ids() -> None:
    """Diagnostics must remain a duplicate-free reference set."""
    diagnostic = SNAPSHOT.phenomenon_discovery.diagnostics[0]
    duplicated = diagnostic.model_copy(
        update={"evidence_ids": [diagnostic.evidence_ids[0], diagnostic.evidence_ids[0]]}
    )
    discovery = SNAPSHOT.phenomenon_discovery.model_copy(
        update={"diagnostics": [duplicated, *SNAPSHOT.phenomenon_discovery.diagnostics[1:]]}
    )
    persisted = SNAPSHOT.model_copy(update={"phenomenon_discovery": discovery})

    with pytest.raises(ValueError, match="duplicate diagnostic evidence ID"):
        validate_snapshot_discovery(persisted)
