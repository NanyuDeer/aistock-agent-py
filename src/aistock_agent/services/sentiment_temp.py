"""短线情绪温度：六指标 → 0-100 温度 + 冰点判定 + 预判生成 + 落盘/加载 + 晨报上下文。"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_quick_think
from aistock_agent.services.market_trace_snapshot import normalize_a_share
from aistock_agent.utils.date import is_trading_day

logger = structlog.get_logger()


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


# ── 预判生成（仅冰点调用）───────────────────────────────────────────

_PREDICTION_FALLBACK = (
    "昨日情绪冰点，短期修复概率较高，关注超跌方向反弹机会，注意弱势板块补跌风险。"
)

_PREDICTION_PROMPT = (
    "你是一名 A 股短线情绪分析师。昨日市场情绪到达冰点"
    "（温度 {score}/100，{level}，连续 {consecutive} 日）。\n"
    "指标：涨停 {up_count} 家，跌停 {down_count} 家，"
    "炸板率 {broken_ratio}，最高连板 {highest_board} 板，"
    "涨跌家数比 {advance_ratio}，主力净流入 {main_force_net_yi} 亿。\n"
    "请输出 1-2 句「冰点次日预判」：短期修复概率（参考历史规律表述，如\"修复概率较高\"）+ "
    "关注方向（超跌反弹方向）+ 风险提示。\n"
    "只输出正文，不要标题、不要列表、不要引号，不超过 60 字。"
)


async def generate_ice_prediction(
    metrics: dict[str, float],
    score: float,
    level: str,
    consecutive: int,
) -> tuple[bool, str]:
    """冰点预判：quick_think 生成；失败/空输出降级为模板话术。"""
    try:
        from langchain_core.messages import HumanMessage

        prompt = _PREDICTION_PROMPT.format(
            score=score,
            level=level,
            consecutive=consecutive,
            up_count=int(metrics.get("up_count", 0)),
            down_count=int(metrics.get("down_count", 0)),
            broken_ratio=metrics.get("broken_ratio", 0.0),
            highest_board=int(metrics.get("highest_board", 0)),
            advance_ratio=metrics.get("advance_ratio", 0.0),
            main_force_net_yi=metrics.get("main_force_net_yi", 0.0),
        )
        resp = await get_quick_think().ainvoke([HumanMessage(content=prompt)])
        text = str(getattr(resp, "content", "") or "").strip()
        if text:
            return True, text
    except Exception:  # noqa: BLE001 —— LLM 失败降级，不阻断任务
        pass
    return False, _PREDICTION_FALLBACK


# ── 落盘 / 加载 ────────────────────────────────────────────────────


def build_sentiment_payload(
    date: str,
    score: float,
    level: str,
    metrics: dict[str, object],
    ice: dict[str, object],
    prediction: dict[str, object],
) -> dict[str, object]:
    """组装落盘 schema（见 spec 5.5）。"""
    return {
        "date": date,
        "is_trading_day": True,
        "score": score,
        "level": level,
        "ice": ice,
        "metrics": metrics,
        "prediction": prediction,
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def persist_sentiment(payload: dict[str, object], output_dir: str) -> None:
    """写当日归档 + latest.json（样式对齐 docs/agent-outputs 惯例）。"""
    root = Path(output_dir)
    date_str = str(payload["date"])
    _write_json(root / f"{date_str}.json", payload)
    _write_json(root / "latest.json", payload)
    logger.info("sentiment_temp_persisted", date=date_str, score=payload.get("score"))


async def load_latest_sentiment(output_dir: str) -> dict[str, object] | None:
    """读 latest.json；缺失或非法 JSON 返回 None。"""
    try:
        raw = Path(output_dir, "latest.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_previous_archive(output_dir: str, report_date: str) -> dict[str, object] | None:
    """严格早于 report_date 的最近归档（连冰计数用）；无 → None。"""
    root = Path(output_dir)
    if not root.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for f in root.glob("????-??-??.json"):
        if f.name == "latest.json":
            continue
        d = f.stem
        if d < report_date:
            candidates.append((d, f))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    try:
        parsed = json.loads(candidates[-1][1].read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _format_score(score: object) -> str:
    """温度展示：整数值省略小数位（52 而非 52.0）。"""
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        return str(score)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def build_morning_sentiment_context(
    latest: dict[str, object] | None,
    extreme_days: int,
) -> str:
    """晨报三档注入文本：冰点→预判+指标；非冰点→一行概览；None→空串。"""
    if not isinstance(latest, dict):
        return ""
    ice = latest.get("ice")
    ice = ice if isinstance(ice, dict) else {}
    score = latest.get("score")
    level = latest.get("level", "")
    date_str = str(latest.get("date", ""))
    if ice.get("is_ice"):
        consecutive = int(ice.get("consecutive_ice_days", 0) or 0)
        consecutive_txt = (
            f"，连续{consecutive}日冰点" if consecutive >= extreme_days else ""
        )
        prediction = latest.get("prediction")
        pred_txt = (
            str(prediction.get("text", ""))
            if isinstance(prediction, dict) and prediction.get("text")
            else ""
        )
        metrics = latest.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        metrics_txt = (
            f" 指标：涨停{metrics.get('up_count', '-')} 跌停{metrics.get('down_count', '-')} "
            f"炸板{metrics.get('broken_count', '-')} 最高连板{metrics.get('highest_board', '-')}。"
        )
        return (
            f"昨日（{date_str}）短线情绪冰点：温度 {_format_score(score)}"
            f"（{level}{consecutive_txt}）。{pred_txt}{metrics_txt}"
        )
    return f"昨日（{date_str}）短线情绪温度 {_format_score(score)}（{level}）。"


# ── 收盘编排（定时任务入口）─────────────────────────────────────────


async def compute_and_persist_sentiment_temp(
    report_date: str,
    output_dir: str | None = None,
) -> dict[str, object] | None:
    """收盘编排：交易日守卫 → close-snapshot → 温度 → 冰点预判 → 落盘。

    Returns:
        落盘的 payload；非交易日、snapshot 缺失或计算异常 → None（不落盘）。
    """
    from datetime import date as date_cls_dt

    try:
        if not is_trading_day(date_cls_dt.fromisoformat(report_date)):
            logger.info("sentiment_temp_skip_non_trading_day", date=report_date)
            return None

        root = output_dir or settings.sentiment_output_dir
        close_data = await node_api.get("/internal/market/close-snapshot")
        if not isinstance(close_data, dict) or close_data.get("status") != "complete":
            logger.warning("sentiment_temp_snapshot_missing", date=report_date)
            return None

        a_share = normalize_a_share(close_data)
        score = compute_sentiment_score(a_share)
        level = sentiment_level(score)

        prev = load_previous_archive(root, report_date)
        prev_ice = prev.get("ice") if isinstance(prev, dict) else {}
        prev_consecutive = (
            int(prev_ice.get("consecutive_ice_days", 0) or 0)
            if isinstance(prev_ice, dict)
            else 0
        )
        ice = judge_ice(
            score,
            prev_consecutive,
            threshold=settings.sentiment_ice_threshold,
            extreme_days=settings.sentiment_ice_consecutive_days,
        )

        metrics = _metrics_payload(a_share, score)
        prediction: dict[str, object] = {"generated": False}
        if ice["is_ice"]:
            generated, text = await generate_ice_prediction(
                metrics, score, level, int(ice["consecutive_ice_days"])
            )
            prediction = {"generated": generated, "text": text}

        payload = build_sentiment_payload(report_date, score, level, metrics, ice, prediction)
        persist_sentiment(payload, root)
        return payload
    except Exception as exc:  # noqa: BLE001 —— 独立任务，失败不阻断调度
        logger.warning("sentiment_temp_task_failed", date=report_date, error=str(exc))
        return None


def _metrics_payload(a_share: dict[str, object], score: float) -> dict[str, object]:
    """落盘 metrics（六指标原值；缺失置 0）。"""
    limits = a_share.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    breadth = a_share.get("breadth")
    breadth = breadth if isinstance(breadth, dict) else {}
    main_force = a_share.get("main_force")
    main_force = main_force if isinstance(main_force, dict) else {}

    def _n(v: object) -> float:
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0

    up = _n(limits.get("up_count"))
    broken = _n(limits.get("broken_count"))
    up_broken = up + broken
    broken_ratio = round(broken / up_broken, 2) if up_broken > 0 else 0.0
    main_force_yi = round(_n(main_force.get("large_and_extra_large_net_yuan")) / 1e8, 1)
    return {
        "up_count": int(_n(limits.get("up_count"))),
        "down_count": int(_n(limits.get("down_count"))),
        "broken_count": int(broken),
        "broken_ratio": broken_ratio,
        "highest_board": int(_n(limits.get("highest_board"))),
        "advance_ratio": round(_n(breadth.get("advance_ratio")), 2),
        "main_force_net_yi": main_force_yi,
    }
