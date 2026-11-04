# src/aistock_agent/services/rhythm_rebuilt_evidence.py
from __future__ import annotations

from datetime import date
from typing import Any, cast, get_args

from aistock_agent.schemas.rhythm_master import Certainty, EventAnchor, PositionBand, Stage
from aistock_agent.utils.date import trading_days_between

_CERTAINTY_SET = {"high", "medium", "low"}
_CONF_SET = {"high", "medium", "low"}

# 中性兜底沿用前一交易日阶段时的固定文案（单一副本：detect_stage 产出、worker 判路径共用，
# 避免两处字面量漂移导致 stage_source 误判）。
INHERIT_PREV_PHASE_REASON = "沿用前阶段（证据中性）"


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


def resolve_prev_phase(
    prev_report: object, *, run_date: str, max_gap_days: int = 3
) -> Stage | None:
    """前一交易日 after_close 卡能否作为 prev_phase（D2 双闸，2026-11-03）。

    背景：detect_stage 在证据中性时沿用前阶段。旧实现只看「stage 是否存在」，
    导致一张错卡可沿链无限传播（10-30 ice → 11-02 沿用 → 11-03 继续）。

    两道闸，任一不满足返回 None（调用方按「无前阶段」处理并留痕）：
    ① 时效：基准日距 run_date ≤ max_gap_days 个交易日（基准日晚于 run_date 也拒绝）；
    ② 非二手：前卡 stage_reason 不得为「沿用前阶段」——继承来的结论没独立证据，
      不得再被继承。

    basis_date 缺失/非法、stage 非法、report 结构残缺 → fail closed 返回 None。
    """
    if not isinstance(prev_report, dict):
        return None
    content = prev_report.get("content")
    if not isinstance(content, dict):
        return None
    evidence = content.get("evidence")
    if not isinstance(evidence, dict):
        return None

    stage = evidence.get("stage")
    if not isinstance(stage, str) or stage not in get_args(Stage):
        return None

    # 闸②：二手结论不得再继承（切断污染链）
    if INHERIT_PREV_PHASE_REASON in str(evidence.get("stage_reason") or ""):
        return None

    # 闸①：基准日时效（格式非法/越年无交易日历 → fail closed）
    basis_raw = content.get("basis_date")
    if not isinstance(basis_raw, str):
        return None
    try:
        basis = date.fromisoformat(basis_raw)
        run = date.fromisoformat(run_date)
    except ValueError:
        return None
    gap = trading_days_between(basis, run)
    if gap is None or gap < 0 or gap > max_gap_days:
        return None
    return cast(Stage, stage)


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
        return prev_phase, INHERIT_PREV_PHASE_REASON
    return None, "证据不足，无前阶段"


# 常量表以模型承载（键为 Stage 闭集，值 action/direction 由 PositionBand 校验为闭集字面量）
_STAGE_POSITION: dict[Stage, PositionBand] = {
    "ice": PositionBand(text="空仓~观察", action="hold", direction="neutral"),
    "launch": PositionBand(text="轻仓~半仓试探", action="add", direction="bullish"),
    "rally": PositionBand(text="6~8 成顺势持有", action="add", direction="bullish"),
    "overheat": PositionBand(text="半仓~减仓防退潮", action="reduce", direction="bearish"),
    "ebb": PositionBand(text="轻仓~观望", action="hold", direction="neutral"),
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
    """DEPRECATED（2026-11-04）：已被 rhythm_engine.derive_position_index 取代，待 rhythm_master 接线后删除。
    """
    if stage is None or certainty is None:
        return None
    base = _STAGE_POSITION[stage]
    # certainty=low 且档位非 reduce → 统一降为 hold（逻辑与旧 dict 取值等价）
    action = base.action
    if certainty == "low" and action != "reduce":
        action = "hold"
    return PositionBand(text=base.text, action=action, direction=base.direction)


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
