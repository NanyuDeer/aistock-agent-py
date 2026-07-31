"""基于冻结市场事实的确定性现象发现。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from aistock_agent.config import settings
from aistock_agent.schemas.market_trace import (
    DataReadiness,
    DetectedPhenomenon,
    MarketPhenomenonKind,
    PhenomenonDiscoveryResult,
    RuleDiagnostic,
    SourceRecord,
)

_RULE_ORDER: tuple[MarketPhenomenonKind, ...] = (
    "broad_rally",
    "broad_decline",
    "style_divergence",
    "sector_concentration",
    "sentiment_extreme",
)

_SUMMARIES: dict[MarketPhenomenonKind, str] = {
    "broad_rally": "多个核心指数同步上涨，市场广度偏强",
    "broad_decline": "多个核心指数同步下跌，市场广度偏弱",
    "style_divergence": "核心指数方向背离，风格分化明显",
    "sector_concentration": "概念板块集中异动，与大盘方向相反",
    "sentiment_extreme": "涨跌停或炸板情绪指标极端",
}


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def classify_causal_evidence(
    sources: dict[str, SourceRecord], captured_at: datetime
) -> Literal["ready", "partial", "not_ready"]:
    """按冻结 SourceRecord 属性判断因果证据是否可用于确认。"""
    has_market_fact = any(record.kind == "market_fact" for record in sources.values())
    if not has_market_fact:
        return "not_ready"
    has_valid_event = any(
        record.kind == "event_evidence"
        and bool(record.url and record.url.strip())
        and record.occurred_at is not None
        and record.occurred_at <= record.captured_at <= captured_at
        for record in sources.values()
    )
    return "ready" if has_valid_event else "partial"


def _has_market_fact(sources: dict[str, SourceRecord], source_id: str) -> bool:
    record = sources.get(source_id)
    return record is not None and record.kind == "market_fact" and record.source_id == source_id


def _index_returns(
    a_share: dict[str, object],
    sources: dict[str, SourceRecord],
) -> dict[str, float]:
    indexes = a_share.get("indexes")
    if not isinstance(indexes, dict):
        return {}
    result: dict[str, float] = {}
    for item in indexes.values():
        if not isinstance(item, dict):
            continue
        ts_code = item.get("ts_code")
        change_pct = item.get("change_pct", item.get("pct_chg"))
        source_id = f"INDEX_{ts_code.replace('.', '_')}" if isinstance(ts_code, str) else ""
        if (
            isinstance(ts_code, str)
            and isinstance(change_pct, int | float)
            and _has_market_fact(sources, source_id)
        ):
            result[ts_code] = float(change_pct)
    return result


def _ordered_real_fact_ids(sources: dict[str, SourceRecord], wanted: set[str]) -> list[str]:
    result = [
        source_id
        for source_id, record in sources.items()
        if source_id in wanted and record.kind == "market_fact"
    ]
    return sorted(result)


def _score_rules(
    a_share: dict[str, object],
    sources: dict[str, SourceRecord],
) -> dict[MarketPhenomenonKind, int]:
    returns = _index_returns(a_share, sources)
    values = list(returns.values())
    if len(values) < 6:
        return {rule: 0 for rule in _RULE_ORDER}

    sorted_values = sorted(values)
    market_median = (sorted_values[2] + sorted_values[3]) / 2
    breadth = _mapping(a_share.get("breadth")) if _has_market_fact(sources, "BREADTH_ALL") else {}
    limits = _mapping(a_share.get("limits")) if _has_market_fact(sources, "LIMITS_ALL") else {}
    turnover = (
        _mapping(a_share.get("turnover")) if _has_market_fact(sources, "TURNOVER_ALL") else {}
    )
    sectors = _mapping(a_share.get("sectors")) if _has_market_fact(sources, "SECTORS_ALL") else {}
    main_force = (
        _mapping(a_share.get("main_force")) if _has_market_fact(sources, "MAIN_FORCE_ALL") else {}
    )

    advance_ratio = _number(breadth.get("advance_ratio"))
    total_count = _number(breadth.get("total_count"))
    decline_ratio = _number(breadth.get("decline_count")) / total_count if total_count else 0.0
    limit_up = _number(limits.get("up_count"))
    limit_down = _number(limits.get("down_count"))
    broken = _number(limits.get("broken_count"))
    highest_board = _number(limits.get("highest_board"))
    turnover_change = _number(turnover.get("change_pct"))
    main_force_net = _number(main_force.get("large_and_extra_large_net_yuan"))

    rally_base = (
        sum(value >= settings.phenomenon_broad_index_change_pct for value in values)
        >= settings.phenomenon_broad_index_count
        and advance_ratio >= settings.phenomenon_broad_breadth_ratio
    ) or all(value >= settings.phenomenon_broad_all_index_change_pct for value in values)
    decline_base = (
        sum(value <= -settings.phenomenon_broad_index_change_pct for value in values)
        >= settings.phenomenon_broad_index_count
        and decline_ratio >= settings.phenomenon_broad_breadth_ratio
    ) or all(value <= -settings.phenomenon_broad_all_index_change_pct for value in values)

    rally_score = 0
    if rally_base:
        rally_score = (
            settings.phenomenon_min_match_score
            + int(limit_up >= limit_down + settings.phenomenon_broad_limit_count_gap)
            + int(turnover_change >= settings.phenomenon_broad_turnover_change_pct)
        )
    decline_score = 0
    if decline_base:
        decline_score = (
            settings.phenomenon_min_match_score
            + int(limit_down >= limit_up + settings.phenomenon_broad_limit_count_gap)
            + int(turnover_change >= settings.phenomenon_broad_turnover_change_pct)
        )
    scores: dict[MarketPhenomenonKind, int] = {
        "broad_rally": rally_score,
        "broad_decline": decline_score,
        "style_divergence": 0,
        "sector_concentration": 0,
        "sentiment_extreme": 0,
    }

    csi300 = returns.get("000300.SH")
    csi1000 = returns.get("000852.SH")
    gainers = _items(sectors.get("top_gainers"))
    losers = _items(sectors.get("top_losers"))
    style_divergence = (
        csi300 is not None
        and csi1000 is not None
        and (
            (
                csi300 >= settings.phenomenon_style_divergence_change_pct
                and csi1000 <= -settings.phenomenon_style_divergence_change_pct
            )
            or (
                csi300 <= -settings.phenomenon_style_divergence_change_pct
                and csi1000 >= settings.phenomenon_style_divergence_change_pct
            )
        )
    )
    scores["style_divergence"] = 2 if style_divergence else 0

    strongest = _number(gainers[0].get("pct_change")) if gainers else 0.0
    weakest = _number(losers[0].get("pct_change")) if losers else 0.0
    direction = 0
    if (
        abs(strongest) >= settings.phenomenon_sector_abs_change_pct
        and strongest * market_median < 0
    ):
        direction = 1 if strongest > 0 else -1
    elif (
        abs(weakest) >= settings.phenomenon_sector_abs_change_pct
        and weakest * market_median < 0
    ):
        direction = 1 if weakest > 0 else -1
    relevant = gainers[:3] if direction > 0 else losers[:3]
    flows_match = bool(relevant) and all(
        _number(item.get("net_amount")) * direction > 0 for item in relevant
    )
    scores["sector_concentration"] = (
        int(direction != 0)
        + int(flows_match)
        + int(
            settings.phenomenon_sector_neutral_breadth_min_ratio
            <= advance_ratio
            <= settings.phenomenon_sector_neutral_breadth_max_ratio
        )
    )

    sentiment_base = (
        limit_up >= settings.phenomenon_sentiment_limit_up_count
        or limit_down >= settings.phenomenon_sentiment_limit_down_count
    ) and (
        broken >= limit_up * settings.phenomenon_sentiment_broken_ratio if limit_up else True
    )
    force_matches = (market_median > 0 and main_force_net > 0) or (
        market_median < 0 and main_force_net < 0
    )
    sentiment_score = 0
    if sentiment_base:
        sentiment_score = (
            1
            + int(highest_board >= settings.phenomenon_sentiment_highest_board)
            + int(force_matches)
        )
    scores["sentiment_extreme"] = sentiment_score
    return scores


def _rule_fact_ids(
    rule: MarketPhenomenonKind,
    a_share: dict[str, object],
    sources: dict[str, SourceRecord],
) -> list[str]:
    returns = _index_returns(a_share, sources)
    threshold = settings.phenomenon_broad_index_change_pct
    if rule == "broad_rally":
        index_ids = {
            f"INDEX_{code.replace('.', '_')}"
            for code, value in returns.items()
            if value >= threshold
        }
        wanted = index_ids | {
            "BREADTH_ALL",
            "TURNOVER_ALL",
            "LIMITS_ALL",
            "MAIN_FORCE_ALL",
        }
    elif rule == "broad_decline":
        index_ids = {
            f"INDEX_{code.replace('.', '_')}"
            for code, value in returns.items()
            if value <= -threshold
        }
        wanted = index_ids | {
            "BREADTH_ALL",
            "TURNOVER_ALL",
            "LIMITS_ALL",
            "MAIN_FORCE_ALL",
        }
    elif rule == "style_divergence":
        wanted = {"INDEX_000300_SH", "INDEX_000852_SH"}
    elif rule == "sector_concentration":
        wanted = {"SECTORS_ALL"}
    else:
        wanted = {"LIMITS_ALL"}
    return _ordered_real_fact_ids(sources, wanted)


def discover_market_phenomenon(
    a_share: dict[str, object],
    sources: dict[str, SourceRecord],
    captured_at: datetime,
    missing_fields: list[str],
) -> PhenomenonDiscoveryResult:
    """在冻结事实上运行规则，并保留真实 source_id 与确定性顺序。"""
    scores = _score_rules(a_share, sources)
    causal_readiness = classify_causal_evidence(sources, captured_at)
    indexes = _index_returns(a_share, sources)
    market_complete = len(indexes) >= 6
    event_count = sum(record.kind == "event_evidence" for record in sources.values())
    attribution_inputs: Literal["complete", "partial", "missing"]
    if event_count == 0:
        attribution_inputs = "missing"
    elif missing_fields:
        attribution_inputs = "partial"
    else:
        attribution_inputs = "complete"
    readiness = DataReadiness(
        market_data="complete" if market_complete else "incomplete",
        attribution_inputs=attribution_inputs,
        causal_evidence=causal_readiness,
    )
    diagnostics = [
        RuleDiagnostic(
            rule=rule,
            matched=scores[rule] >= settings.phenomenon_min_match_score,
            evidence_ids=_rule_fact_ids(rule, a_share, sources),
        )
        for rule in _RULE_ORDER
    ]
    if not market_complete:
        return PhenomenonDiscoveryResult(
            status="insufficient_data",
            primary=None,
            concurrent_phenomena=[],
            data_readiness=readiness,
            diagnostics=diagnostics,
        )

    matched = [rule for rule in _RULE_ORDER if scores[rule] >= settings.phenomenon_min_match_score]
    if not matched:
        return PhenomenonDiscoveryResult(
            status="no_phenomenon",
            primary=None,
            concurrent_phenomena=[],
            data_readiness=readiness,
            diagnostics=diagnostics,
        )
    matched.sort(key=lambda rule: (-scores[rule], _RULE_ORDER.index(rule)))

    def detected(rule: MarketPhenomenonKind) -> DetectedPhenomenon:
        score = scores[rule]
        return DetectedPhenomenon(
            kind=rule,
            summary=_SUMMARIES[rule],
            fact_ids=_rule_fact_ids(rule, a_share, sources),
            tags=[rule],
            severity="high" if score >= settings.phenomenon_high_severity_score else "medium",
        )

    primary = detected(matched[0])
    concurrent = [detected(rule) for rule in matched[1:]]
    return PhenomenonDiscoveryResult(
        status="detected",
        primary=primary,
        concurrent_phenomena=concurrent,
        data_readiness=readiness,
        diagnostics=diagnostics,
    )
