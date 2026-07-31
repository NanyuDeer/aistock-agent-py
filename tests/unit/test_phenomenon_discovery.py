"""冻结现象发现只使用真实、有序的市场事实来源。"""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from aistock_agent.schemas import market_trace as market_trace_schema
from aistock_agent.schemas.market_trace import SourceRecord
from aistock_agent.services.phenomenon_discovery import (
    classify_causal_evidence,
    discover_market_phenomenon,
)

_CAPTURED_AT = datetime(2026, 7, 19, 15, 30, tzinfo=UTC)


def test_phenomenon_kind_alias_uses_neutral_name() -> None:
    assert hasattr(market_trace_schema, "MarketPhenomenonKind")
    assert not hasattr(market_trace_schema, "DominantPhenomenonKind")


def _source(
    source_id: str,
    *,
    kind: str = "market_fact",
    url: str | None = None,
    occurred_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        kind=kind,
        provider="test",
        title=source_id,
        content="fact",
        url=url,
        occurred_at=occurred_at or _CAPTURED_AT,
        captured_at=captured_at or _CAPTURED_AT,
        source_level="reporting" if kind == "event_evidence" else "market_data",
    )


def _rally_facts() -> dict[str, object]:
    return {
        "indexes": {
            "SH000001": {"ts_code": "000001.SH", "pct_chg": 1.2},
            "SZ399001": {"ts_code": "399001.SZ", "pct_chg": 1.5},
            "SZ399006": {"ts_code": "399006.SZ", "pct_chg": 1.8},
            "SH000300": {"ts_code": "000300.SH", "pct_chg": 1.0},
            "SH000905": {"ts_code": "000905.SH", "pct_chg": 0.9},
            "SH000852": {"ts_code": "000852.SH", "pct_chg": 1.1},
        },
        "breadth": {
            "advance_ratio": 0.75,
            "total_count": 5000,
            "decline_count": 1000,
        },
        "limits": {
            "up_count": 50,
            "down_count": 10,
            "broken_count": 5,
            "highest_board": 3,
        },
        "turnover": {"change_pct": 15.0},
        "sectors": {"top_gainers": [], "top_losers": []},
        "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
    }


_INDEX_SOURCE_IDS = [
    "INDEX_000001_SH",
    "INDEX_399001_SZ",
    "INDEX_399006_SZ",
    "INDEX_000300_SH",
    "INDEX_000905_SH",
    "INDEX_000852_SH",
]
_AGGREGATE_SOURCE_IDS = [
    "BREADTH_ALL",
    "TURNOVER_ALL",
    "LIMITS_ALL",
    "MAIN_FORCE_ALL",
    "SECTORS_ALL",
]


def _all_market_sources() -> dict[str, SourceRecord]:
    return {
        source_id: _source(source_id) for source_id in [*_INDEX_SOURCE_IDS, *_AGGREGATE_SOURCE_IDS]
    }


def _neutral_facts() -> dict[str, object]:
    facts = copy.deepcopy(_rally_facts())
    indexes = facts["indexes"]
    assert isinstance(indexes, dict)
    for index in indexes.values():
        assert isinstance(index, dict)
        index["change_pct"] = 0.0
    facts["breadth"] = {
        "advance_ratio": 0.5,
        "total_count": 5000,
        "decline_count": 2500,
    }
    facts["limits"] = {
        "up_count": 10,
        "down_count": 10,
        "broken_count": 1,
        "highest_board": 2,
    }
    facts["turnover"] = {"change_pct": 0.0}
    facts["sectors"] = {"top_gainers": [], "top_losers": []}
    facts["main_force"] = {"large_and_extra_large_net_yuan": 0}
    return facts


def _set_index_change(facts: dict[str, object], key: str, value: float) -> None:
    indexes = facts["indexes"]
    assert isinstance(indexes, dict)
    index = indexes[key]
    assert isinstance(index, dict)
    index["change_pct"] = value


def _diagnostic_ids(discovery: object, rule: str) -> list[str]:
    diagnostics = getattr(discovery, "diagnostics")
    return next(item.evidence_ids for item in diagnostics if item.rule == rule)


def test_classify_causal_evidence_reads_source_record_attributes() -> None:
    sources = {
        "INDEX_000001_SH": _source("INDEX_000001_SH"),
        "NEWS_001": _source(
            "NEWS_001",
            kind="event_evidence",
            url="https://example.com/event",
            occurred_at=_CAPTURED_AT - timedelta(minutes=5),
        ),
    }

    assert classify_causal_evidence(sources, _CAPTURED_AT) == "ready"


def test_classify_causal_evidence_rejects_late_or_untraceable_events() -> None:
    market = _source("INDEX_000001_SH")
    no_url = _source("NEWS_001", kind="event_evidence", occurred_at=_CAPTURED_AT)
    late = _source(
        "NEWS_002",
        kind="event_evidence",
        url="https://example.com/late",
        occurred_at=_CAPTURED_AT + timedelta(seconds=1),
    )

    assert (
        classify_causal_evidence({market.source_id: market, no_url.source_id: no_url}, _CAPTURED_AT)
        == "partial"
    )
    assert (
        classify_causal_evidence({market.source_id: market, late.source_id: late}, _CAPTURED_AT)
        == "partial"
    )
    assert classify_causal_evidence({no_url.source_id: no_url}, _CAPTURED_AT) == "not_ready"


def test_classify_causal_evidence_rejects_event_after_source_capture() -> None:
    market = _source("INDEX_000001_SH")
    future_at_capture = _source(
        "NEWS_001",
        kind="event_evidence",
        url="https://example.com/future-at-capture",
        occurred_at=_CAPTURED_AT - timedelta(minutes=1),
        captured_at=_CAPTURED_AT - timedelta(minutes=2),
    )

    assert (
        classify_causal_evidence(
            {market.source_id: market, future_at_capture.source_id: future_at_capture},
            _CAPTURED_AT,
        )
        == "partial"
    )


def test_discovery_uses_only_real_market_fact_ids_in_source_order() -> None:
    source_ids = [
        "INDEX_000001_SH",
        "INDEX_399001_SZ",
        "INDEX_399006_SZ",
        "INDEX_000300_SH",
        "INDEX_000905_SH",
        "INDEX_000852_SH",
        "BREADTH_ALL",
        "TURNOVER_ALL",
        "LIMITS_ALL",
        "MAIN_FORCE_ALL",
        "SECTORS_ALL",
    ]
    sources = {source_id: _source(source_id) for source_id in source_ids}
    sources["NEWS_001"] = _source(
        "NEWS_001",
        kind="event_evidence",
        url="https://example.com/event",
        occurred_at=_CAPTURED_AT,
    )

    discovery = discover_market_phenomenon(_rally_facts(), sources, _CAPTURED_AT, [])

    assert discovery.status == "detected"
    assert discovery.primary is not None
    assert discovery.primary.kind == "broad_rally"
    assert discovery.primary.fact_ids == source_ids[:10]
    diagnostic = next(item for item in discovery.diagnostics if item.rule == "broad_rally")
    assert diagnostic.evidence_ids == source_ids[:10]
    assert all(not source_id.startswith("RULE_") for source_id in diagnostic.evidence_ids)


def test_broad_rally_does_not_match_below_all_thresholds() -> None:
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, 0.79)
    facts["breadth"] = {
        "advance_ratio": 0.54,
        "total_count": 5000,
        "decline_count": 2200,
    }
    facts["limits"] = {
        "up_count": 29,
        "down_count": 10,
        "broken_count": 1,
        "highest_board": 2,
    }
    facts["turnover"] = {"change_pct": 9.9}

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "no_phenomenon"
    assert (
        next(item for item in discovery.diagnostics if item.rule == "broad_rally").matched is False
    )


def test_broad_decline_binds_real_sources_in_frozen_order() -> None:
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, -1.2)
    facts["breadth"] = {
        "advance_ratio": 0.2,
        "total_count": 5000,
        "decline_count": 4000,
    }
    facts["limits"] = {
        "up_count": 10,
        "down_count": 50,
        "broken_count": 5,
        "highest_board": 3,
    }
    facts["turnover"] = {"change_pct": 15.0}
    facts["main_force"] = {"large_and_extra_large_net_yuan": -5_000_000_000}

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "detected"
    assert discovery.primary is not None
    assert discovery.primary.kind == "broad_decline"
    expected_ids = [
        *_INDEX_SOURCE_IDS,
        "BREADTH_ALL",
        "TURNOVER_ALL",
        "LIMITS_ALL",
        "MAIN_FORCE_ALL",
    ]
    assert discovery.primary.fact_ids == expected_ids
    assert _diagnostic_ids(discovery, "broad_decline") == expected_ids


def test_broad_decline_does_not_match_below_all_thresholds() -> None:
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, -0.79)
    facts["breadth"] = {
        "advance_ratio": 0.46,
        "total_count": 5000,
        "decline_count": 2699,
    }
    facts["limits"] = {
        "up_count": 10,
        "down_count": 29,
        "broken_count": 1,
        "highest_board": 2,
    }
    facts["turnover"] = {"change_pct": 9.9}

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "no_phenomenon"
    assert (
        next(item for item in discovery.diagnostics if item.rule == "broad_decline").matched
        is False
    )


def test_style_divergence_uses_only_csi300_and_csi1000_at_opposite_thresholds() -> None:
    facts = _neutral_facts()
    _set_index_change(facts, "SH000300", 0.5)
    _set_index_change(facts, "SH000852", -0.5)

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "detected"
    assert discovery.primary is not None
    assert discovery.primary.kind == "style_divergence"
    expected_ids = ["INDEX_000300_SH", "INDEX_000852_SH"]
    assert discovery.primary.fact_ids == expected_ids
    assert _diagnostic_ids(discovery, "style_divergence") == expected_ids


def test_style_divergence_rejects_same_direction_designated_indexes() -> None:
    facts = _neutral_facts()
    _set_index_change(facts, "SH000300", 0.5)
    _set_index_change(facts, "SH000852", 0.5)

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "no_phenomenon"


def test_style_divergence_rejects_other_index_and_sector_reversals() -> None:
    facts = _neutral_facts()
    _set_index_change(facts, "SH000001", 1.0)
    _set_index_change(facts, "SZ399001", -1.0)
    _set_index_change(facts, "SH000300", 0.1)
    _set_index_change(facts, "SH000852", -0.1)
    facts["sectors"] = {
        "top_gainers": [{"pct_change": 3.0, "net_amount": 100}],
        "top_losers": [{"pct_change": -3.0, "net_amount": -100}],
    }

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "no_phenomenon"
    assert (
        next(item for item in discovery.diagnostics if item.rule == "style_divergence").matched
        is False
    )


def test_style_divergence_rejects_missing_designated_source() -> None:
    facts = _neutral_facts()
    _set_index_change(facts, "SH000001", 1.0)
    _set_index_change(facts, "SZ399001", -1.0)
    _set_index_change(facts, "SH000300", 0.5)
    _set_index_change(facts, "SH000852", -0.5)
    facts["sectors"] = {
        "top_gainers": [{"pct_change": 3.0, "net_amount": 100}],
        "top_losers": [{"pct_change": -3.0, "net_amount": -100}],
    }
    sources = _all_market_sources()
    sources.pop("INDEX_000852_SH")

    discovery = discover_market_phenomenon(facts, sources, _CAPTURED_AT, [])

    assert discovery.status != "detected" or all(
        item.kind != "style_divergence"
        for item in [discovery.primary, *discovery.concurrent_phenomena]
        if item is not None
    )


def test_sector_concentration_binds_sector_source_and_respects_three_percent_threshold() -> None:
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, 0.2)
    facts["sectors"] = {
        "top_gainers": [],
        "top_losers": [{"pct_change": -3.0, "net_amount": -100}],
    }

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "detected"
    assert discovery.primary is not None
    assert discovery.primary.kind == "sector_concentration"
    assert discovery.primary.fact_ids == ["SECTORS_ALL"]
    assert _diagnostic_ids(discovery, "sector_concentration") == ["SECTORS_ALL"]

    facts["sectors"] = {
        "top_gainers": [],
        "top_losers": [{"pct_change": -2.99, "net_amount": -100}],
    }
    below = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])
    assert below.status == "no_phenomenon"


def test_sentiment_extreme_binds_only_limits_and_respects_thresholds() -> None:
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, 0.1)
    facts["limits"] = {
        "up_count": 50,
        "down_count": 10,
        "broken_count": 18,
        "highest_board": 5,
    }
    facts["main_force"] = {"large_and_extra_large_net_yuan": 1}

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    assert discovery.status == "detected"
    assert discovery.primary is not None
    assert discovery.primary.kind == "sentiment_extreme"
    expected_ids = ["LIMITS_ALL"]
    assert discovery.primary.fact_ids == expected_ids
    assert _diagnostic_ids(discovery, "sentiment_extreme") == expected_ids

    facts["limits"] = {
        "up_count": 49,
        "down_count": 29,
        "broken_count": 0,
        "highest_board": 4,
    }
    below = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])
    assert below.status == "no_phenomenon"


@pytest.mark.parametrize("kind", ["broad_rally", "broad_decline"])
def test_broad_rules_require_all_six_frozen_index_sources(kind: str) -> None:
    facts = copy.deepcopy(_rally_facts())
    if kind == "broad_decline":
        indexes = facts["indexes"]
        assert isinstance(indexes, dict)
        for index in indexes.values():
            assert isinstance(index, dict)
            index["change_pct"] = -1.5
        facts["breadth"] = {
            "advance_ratio": 0.2,
            "total_count": 5000,
            "decline_count": 4000,
        }
        facts["limits"] = {
            "up_count": 10,
            "down_count": 50,
            "broken_count": 5,
            "highest_board": 3,
        }
    sources = _all_market_sources()
    sources.pop("INDEX_000852_SH")

    discovery = discover_market_phenomenon(facts, sources, _CAPTURED_AT, [])

    assert discovery.status == "insufficient_data"


@pytest.mark.parametrize(
    ("kind", "missing_source"),
    [("sector_concentration", "SECTORS_ALL"), ("sentiment_extreme", "LIMITS_ALL")],
)
def test_specialized_rules_require_their_frozen_aggregate_source(
    kind: str,
    missing_source: str,
) -> None:
    facts = _neutral_facts()
    if kind == "sector_concentration":
        for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
            _set_index_change(facts, key, 0.2)
        facts["sectors"] = {
            "top_gainers": [],
            "top_losers": [{"pct_change": -3.0, "net_amount": -100}],
        }
    else:
        for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
            _set_index_change(facts, key, 0.1)
        facts["limits"] = {
            "up_count": 50,
            "down_count": 10,
            "broken_count": 18,
            "highest_board": 5,
        }
        facts["main_force"] = {"large_and_extra_large_net_yuan": 1}
    sources = _all_market_sources()
    sources.pop(missing_source)

    discovery = discover_market_phenomenon(facts, sources, _CAPTURED_AT, [])

    assert discovery.status != "detected" or all(
        item.kind != kind
        for item in [discovery.primary, *discovery.concurrent_phenomena]
        if item is not None
    )


def test_discovery_is_deterministic() -> None:
    sources = {
        source_id: _source(source_id)
        for source_id in (
            "INDEX_000001_SH",
            "INDEX_399001_SZ",
            "INDEX_399006_SZ",
            "INDEX_000300_SH",
            "INDEX_000905_SH",
            "INDEX_000852_SH",
            "BREADTH_ALL",
            "TURNOVER_ALL",
            "LIMITS_ALL",
            "MAIN_FORCE_ALL",
        )
    }

    first = discover_market_phenomenon(_rally_facts(), sources, _CAPTURED_AT, [])
    second = discover_market_phenomenon(_rally_facts(), sources, _CAPTURED_AT, [])

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_discovery_never_uses_nonexistent_or_event_fact_ids() -> None:
    sources = {
        "INDEX_000001_SH": _source("INDEX_000001_SH"),
        "BREADTH_ALL": _source("BREADTH_ALL"),
        "NEWS_001": _source(
            "NEWS_001",
            kind="event_evidence",
            url="https://example.com/event",
            occurred_at=_CAPTURED_AT,
        ),
    }

    discovery = discover_market_phenomenon(_rally_facts(), sources, _CAPTURED_AT, [])

    assert {item.rule: item.evidence_ids for item in discovery.diagnostics} == {
        "broad_rally": ["INDEX_000001_SH", "BREADTH_ALL"],
        "broad_decline": ["BREADTH_ALL"],
        "style_divergence": [],
        "sector_concentration": [],
        "sentiment_extreme": [],
    }


def test_broad_rally_requires_index_and_breadth_base() -> None:
    """辅助信号（涨跌停差、成交额变化）不能在 broad_rally 基础条件不成立时独立命中。"""
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, 0.1)
    facts["limits"] = {"up_count": 50, "down_count": 10, "broken_count": 0, "highest_board": 3}
    facts["turnover"] = {"change_pct": 15.0}

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    diag = next(item for item in discovery.diagnostics if item.rule == "broad_rally")
    assert diag.matched is False


def test_sentiment_extreme_requires_limit_base() -> None:
    """辅助信号（最高连板、同向资金）不能在 sentiment_extreme 基础条件不成立时独立命中。"""
    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, 0.1)
    facts["limits"] = {"up_count": 49, "down_count": 29, "broken_count": 0, "highest_board": 5}
    facts["main_force"] = {"large_and_extra_large_net_yuan": 1}

    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])

    diag = next(item for item in discovery.diagnostics if item.rule == "sentiment_extreme")
    assert diag.matched is False


def test_phenomenon_thresholds_are_config_driven(monkeypatch) -> None:
    """修改 config 阈值应改变检测结果—放宽 broad_rally 指数阈值后原不命中的 facts 变为 detected。"""
    from aistock_agent.config import settings

    facts = _neutral_facts()
    for key in ("SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905", "SH000852"):
        _set_index_change(facts, key, 0.5)
    facts["breadth"] = {
        "advance_ratio": 0.60,
        "total_count": 5000,
        "decline_count": 2000,
    }
    facts["limits"] = {"up_count": 50, "down_count": 10, "broken_count": 0, "highest_board": 3}
    facts["turnover"] = {"change_pct": 15.0}

    # 默认阈值：index_change=0.8, index_count=4，指数 0.5 < 0.8，不命中
    discovery = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])
    diag = next(item for item in discovery.diagnostics if item.rule == "broad_rally")
    assert diag.matched is False

    # 放宽 index_change_pct 到 0.4，则 0.5 >= 0.4 且 count=6 >= 4，应命中
    monkeypatch.setattr(settings, "phenomenon_broad_index_change_pct", 0.4)
    discovery2 = discover_market_phenomenon(facts, _all_market_sources(), _CAPTURED_AT, [])
    diag2 = next(item for item in discovery2.diagnostics if item.rule == "broad_rally")
    assert diag2.matched is True


def test_discovery_returns_insufficient_data_without_complete_market_facts() -> None:
    discovery = discover_market_phenomenon({}, {}, _CAPTURED_AT, ["a_share.indexes"])

    assert discovery.status == "insufficient_data"
    assert discovery.primary is None
    assert discovery.concurrent_phenomena == []
    assert discovery.data_readiness.market_data == "incomplete"
