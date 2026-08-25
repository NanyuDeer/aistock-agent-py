"""短线情绪温度：六指标 → 0-100 温度 + 冰点判定（纯函数，无 IO）。"""

from __future__ import annotations


def _num(value: object) -> float:
    """安全取数值，非数值/None 返回 0。"""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _segment(value: float, segments: list[tuple[float, float]]) -> float:
    """分段映射：segments 为 [(下界, 分数)]，取 value >= 下界 的最大档；否则取最末档。"""
    for lo, score in segments:
        if value >= lo:
            return score
    return segments[-1][1]


_UP_COUNT_SEGMENTS = [(80, 100.0), (50, 80.0), (30, 60.0), (20, 40.0), (10, 20.0), (0, 10.0)]
_DOWN_COUNT_SEGMENTS = [(100, 0.0), (50, 10.0), (30, 20.0), (15, 35.0), (5, 60.0), (0, 90.0)]
_BROKEN_RATIO_SEGMENTS = [(0.6, 20.0), (0.4, 40.0), (0.25, 60.0), (0.15, 80.0), (0, 90.0)]
_BOARD_SEGMENTS = [(8, 100.0), (5, 85.0), (3, 70.0), (2, 55.0), (1, 40.0), (0, 20.0)]
_ADVANCE_RATIO_SEGMENTS = [(0.7, 90.0), (0.55, 70.0), (0.4, 50.0), (0.25, 30.0), (0, 15.0)]

# 六指标权重（和为 1.0）
_WEIGHTS: dict[str, float] = {
    "up_count": 0.25,
    "down_count": 0.25,
    "broken_ratio": 0.15,
    "highest_board": 0.15,
    "advance_ratio": 0.15,
    "main_force": 0.05,
}


def _indicator_scores(a_share: dict[str, object]) -> dict[str, float]:
    """六指标各自 0-100 分；字段缺失 → 中性 50。"""
    limits = a_share.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    breadth = a_share.get("breadth")
    breadth = breadth if isinstance(breadth, dict) else {}
    main_force = a_share.get("main_force")
    main_force = main_force if isinstance(main_force, dict) else {}

    up = _num(limits.get("up_count"))
    down = _num(limits.get("down_count"))
    broken = _num(limits.get("broken_count"))
    up_broken = up + broken
    broken_ratio = broken / up_broken if up_broken > 0 else 0.5

    scores = {
        "up_count": _segment(up, _UP_COUNT_SEGMENTS) if "up_count" in limits else 50.0,
        "down_count": _segment(down, _DOWN_COUNT_SEGMENTS) if "down_count" in limits else 50.0,
        "broken_ratio": _segment(broken_ratio, _BROKEN_RATIO_SEGMENTS)
        if any(k in limits for k in ("up_count", "down_count", "broken_count"))
        else 50.0,
        "highest_board": _segment(_num(limits.get("highest_board")), _BOARD_SEGMENTS)
        if "highest_board" in limits
        else 50.0,
        "advance_ratio": _segment(_num(breadth.get("advance_ratio")), _ADVANCE_RATIO_SEGMENTS)
        if "advance_ratio" in breadth
        else 50.0,
        "main_force": _main_force_score(main_force),
    }
    return scores


def _main_force_score(main_force: dict[str, object]) -> float:
    """主力净额（元 → 亿）→ 0-100；缺失中性 50。"""
    raw = main_force.get("large_and_extra_large_net_yuan")
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return 50.0
    net_yi = raw / 1e8
    if net_yi > 0:
        return 65.0
    if net_yi > -50:
        return 45.0
    return 25.0


def compute_sentiment_score(a_share: dict[str, object]) -> float:
    """六指标加权 → 0-100 温度（1 位小数，clamp 0-100）。"""
    scores = _indicator_scores(a_share)
    total = sum(scores[key] * _WEIGHTS[key] for key in _WEIGHTS)
    return round(max(0.0, min(100.0, total)), 1)


def sentiment_level(score: float) -> str:
    """温度分档：冰点 ≤20 / 低迷 (20,45] / 常温 (45,55] / 活跃 (55,80] / 亢奋 >80。"""
    if score <= 20:
        return "冰点"
    if score <= 45:
        return "低迷"
    if score <= 55:
        return "常温"
    if score <= 80:
        return "活跃"
    return "亢奋"


def judge_ice(
    score: float,
    prev_consecutive_ice_days: int,
    threshold: int,
    extreme_days: int,
) -> dict[str, object]:
    """冰点判定：score ≤ threshold 判冰点；连冰天数在前值基础上累计；≥ extreme_days 升级。"""
    is_ice = score <= threshold
    consecutive = (prev_consecutive_ice_days + 1) if is_ice else 0
    return {
        "is_ice": is_ice,
        "consecutive_ice_days": consecutive,
        "is_extreme_ice": is_ice and consecutive >= extreme_days,
    }
