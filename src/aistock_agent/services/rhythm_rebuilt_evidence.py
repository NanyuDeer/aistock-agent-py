# src/aistock_agent/services/rhythm_rebuilt_evidence.py
from __future__ import annotations

from typing import Any

from aistock_agent.schemas.rhythm_master import Certainty, EventAnchor, PositionBand, Stage

_CERTAINTY_SET = {"high", "medium", "low"}
_CONF_SET = {"high", "medium", "low"}


def _advance_ratio(breadth: dict[str, Any] | None) -> float | None:
    if not breadth:
        return None
    total = breadth.get("total_count")
    advance = breadth.get("advance_count")
    if not isinstance(total, int) or not isinstance(advance, int) or total <= 0:
        return None
    return advance / total


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _trend_score(closes: list[float]) -> float:
    if len(closes) < 20:
        return 0.0
    c = closes[-1]
    ma5, ma10, ma20 = _sma(closes, 5), _sma(closes, 10), _sma(closes, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return 0.0
    score = 0.0
    if c > ma5 > ma10 > ma20:
        score += 1.0
    elif c < ma5 < ma10 < ma20:
        score -= 1.0
    score += 0.5 if c > ma20 else -0.5
    return score


def _volume_score(amounts: list[float]) -> float:
    if len(amounts) < 20:
        return 0.0
    avg20, avg5 = _sma(amounts, 20), _sma(amounts, 5)
    if avg20 is None or avg5 is None or avg20 <= 0:
        return 0.0
    ratio = avg5 / avg20
    if ratio >= 1.1:
        return 1.0
    if ratio <= 0.8:
        return -1.0
    return 0.0


def _sentiment_score(sentiment: list[float]) -> float:
    if len(sentiment) < 2:
        return 0.0
    slope = sentiment[-1] - sentiment[0]
    if slope > 5 and sentiment[-1] >= 40:
        return 1.0
    if slope < -5 and sentiment[-1] <= 25:
        return -1.0
    return 0.0


def _fg_score(fg: float | None) -> float:
    if fg is None:
        return 0.0
    if fg >= 60:
        return 1.0
    if fg <= 30:
        return -1.0
    return 0.0


def detect_stage(
    *,
    breadth: dict[str, Any] | None,
    closes: list[float],
    amounts: list[float],
    sentiment_scores: list[float],
    fg: float | None,
    prev_phase: Stage | None,
) -> tuple[Stage | None, str]:
    trend = _trend_score(closes)
    vol = _volume_score(amounts)
    senti = _sentiment_score(sentiment_scores)
    fg_s = _fg_score(fg)
    ratio = _advance_ratio(breadth)
    breadth_s = 0.0
    if ratio is not None:
        if ratio >= 0.6:
            breadth_s = 1.0
        elif ratio <= 0.35:
            breadth_s = -1.0
    score = trend + vol + senti + fg_s + breadth_s

    hot_sentiment = sentiment_scores[-1] >= 70 if sentiment_scores else False
    if score >= 3 and trend >= 1.0 and breadth_s >= 1.0:
        return "rally", "宽度扩张+量能健康+均线多头，主升"
    if score >= 1 and trend >= 0.5 and vol >= 0:
        return "launch", "情绪回升+量能配合，启动"
    if score <= -2 or (ratio is not None and ratio <= 0.35 and vol <= -1.0):
        return "ice", "宽度收缩+量能萎缩，冰点筑底"
    if hot_sentiment and trend <= -0.5:
        return "overheat", "情绪过热但趋势转弱，过热"
    if score <= -0.5:
        return "ebb", "温度回落/量能转弱，退潮"
    if prev_phase is not None:
        return prev_phase, "沿用前阶段（证据中性）"
    return None, "证据不足，无前阶段"


_STAGE_POSITION: dict[str, dict[str, str]] = {
    "ice": {"text": "空仓~观察", "action": "hold", "direction": "neutral"},
    "launch": {"text": "轻仓~半仓试探", "action": "add", "direction": "bullish"},
    "rally": {"text": "6~8 成顺势持有", "action": "add", "direction": "bullish"},
    "overheat": {"text": "半仓~减仓防退潮", "action": "reduce", "direction": "bearish"},
    "ebb": {"text": "轻仓~观望", "action": "hold", "direction": "neutral"},
}


def detect_certainty(
    *,
    event_confirm: bool,
    volume_direction: str | None,
    stage: Stage | None,
    breadth: dict[str, Any] | None,
) -> tuple[Certainty | None, str]:
    ratio = _advance_ratio(breadth)
    breadth_confirm = ratio is not None and ratio >= 0.6
    confirms = sum([
        event_confirm,
        volume_direction in {"bullish", "bearish"},
        breadth_confirm,
    ])
    if confirms >= 2 and stage in {"launch", "rally"}:
        return "high", "事件确认+量价+宽度共振"
    if confirms >= 1:
        return "medium", "部分维度确认，待更多信号"
    return "low", "多空未共振，观望"


def compute_position(
    *, stage: Stage | None, certainty: Certainty | None,
) -> PositionBand | None:
    if stage is None or certainty is None:
        return None
    base = _STAGE_POSITION[stage]
    if certainty == "low":
        action = "hold" if base["action"] != "reduce" else "reduce"
    else:
        action = base["action"]
    return PositionBand(text=base["text"], action=action, direction=base["direction"])


def build_event_anchors(events: list[dict[str, Any]]) -> list[EventAnchor]:
    anchors: list[EventAnchor] = []
    for e in events:
        if e.get("importance") != "high":
            continue
        date_str = str(e.get("date") or "")
        title = str(e.get("title") or "")
        if not date_str or not title:
            continue
        anchors.append(
            EventAnchor(
                event_date=date_str,
                title=title,
                confirm_condition="公布后按预期差确认",
            )
        )
    return anchors
