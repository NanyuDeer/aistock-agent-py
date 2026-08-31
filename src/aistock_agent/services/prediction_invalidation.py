"""预测失效条件"读数触发式"复核触发器 — 三态迟滞状态机（A1）。"""

from __future__ import annotations

from typing import Any

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.services.prediction_targets import INDEX_TARGETS
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()


def update_trigger_state(
    prev: dict[str, Any],
    today_below: bool,
    *,
    arm_days: int = 2,
    release_days: int = 3,
) -> tuple[str, dict[str, Any]]:
    """三态迟滞状态机。prev 为上次持久化 dict {state, below_streak, above_streak}。

    - inactive：today_below 连续 arm_days 日 → armed
    - armed：继续跌破保持；收复且 above_streak < release_days → de_escalating
    - de_escalating：重新跌破 → armed；above_streak 达 release_days → inactive
    单日抖动不迁移（计数仅在方向持续时累计）。
    """
    state = str(prev.get("state") or "inactive")
    below = int(prev.get("below_streak") or 0)
    above = int(prev.get("above_streak") or 0)

    if state == "inactive":
        if today_below:
            below += 1
            if below >= arm_days:
                return "armed", {"state": "armed", "below_streak": below, "above_streak": 0}
            return "inactive", {"state": "inactive", "below_streak": below, "above_streak": 0}
        return "inactive", {"state": "inactive", "below_streak": 0, "above_streak": 0}

    if state == "armed":
        if today_below:
            return "armed", {"state": "armed", "below_streak": below + 1, "above_streak": 0}
        above += 1
        return "de_escalating", {"state": "de_escalating", "below_streak": 0, "above_streak": above}

    # de_escalating
    if today_below:
        return "armed", {"state": "armed", "below_streak": 1, "above_streak": 0}
    above += 1
    if above >= release_days:
        return "inactive", {"state": "inactive", "below_streak": 0, "above_streak": 0}
    return "de_escalating", {"state": "de_escalating", "below_streak": 0, "above_streak": above}


def _ma(closes: list[float], window: int) -> float | None:
    """最近 window 根收盘的简单均值；不足 window 根返回 None（无法判定）。"""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _index_code(rec: dict[str, Any]) -> str | None:
    """从 prediction.horizons 目标解析指数代码；非指数目标返回 None。

    真实记录行无顶层 target_code（代码在 prediction.horizons 内嵌的 target 上），
    取首个可解析指数目标的代码（v1 按单指数粒度扫描；sector 目标需板块 kline，
    属后续任务范围，此处跳过）。
    """
    prediction = rec.get("prediction")
    if not isinstance(prediction, dict):
        return None
    horizons = prediction.get("horizons")
    if not isinstance(horizons, list):
        return None
    for h in horizons:
        if isinstance(h, dict):
            code = INDEX_TARGETS.get(str(h.get("target") or ""))
            if code:
                return code
    return None


async def scan_active_pending() -> list[str]:
    """每日一次扫描未到期 pending，推进三态迟滞状态机（A1）。

    仅处理指数目标（v1 范围）。状态跨日持久化：有任何信号（prev 非空 /
    今日跌破 / 状态非 inactive）就经 update_prediction_verification 回写
    early_exit 状态 dict（early_exit-only entry 无 result，Node 端不联动
    status=verified）；仅当 inactive→armed 当日返回该记录 id（新触发列表，
    供上层提醒接线）。

    Returns:
        新触发（inactive→armed）的 prediction_id 字符串列表。
    """
    records = await node_api.list_pending_predictions()
    if not records:
        return []
    triggered: list[str] = []
    today = shanghai_today().isoformat()
    for rec in records:
        code = _index_code(rec)
        if code is None:
            continue  # 非指数目标：v1 不监控（sector 需板块 kline，后续任务）
        kline = await node_api.get_index_kline(code, days=130)
        if not kline:
            continue  # 数据源故障：跳过该记录，不抛异常，下轮再扫
        closes = [float(r["close"]) for r in kline if r.get("close") is not None]
        if len(closes) < 21:
            continue
        ma20 = _ma(closes, 20)
        today_below = bool(ma20 is not None and closes[-1] < ma20)
        verification = rec.get("verification")
        if not isinstance(verification, dict):
            verification = {}
        due_dates = rec.get("due_dates")
        horizons = list(due_dates.keys()) if isinstance(due_dates, dict) else []
        for horizon in horizons:
            entry = verification.get(horizon)
            prev = entry.get("early_exit") if isinstance(entry, dict) else {}
            if not isinstance(prev, dict):
                prev = {}
            state, persist = update_trigger_state(prev, today_below)
            if not (prev or today_below or state != "inactive"):
                continue  # 无信号（inactive 且从未跌破）：不写状态
            early_exit: dict[str, Any] = {
                **persist,
                "indicator": "ma20",
                "snapshot_value": ma20,
            }
            if state == "armed" and str(prev.get("state")) != "armed":
                # inactive→armed 首日：记录触发时间并返回新触发 id
                early_exit["triggered_at"] = today
                triggered.append(str(rec["id"]))
            try:
                await node_api.update_prediction_verification(
                    rec["id"], horizon, {"type": "early_exit", "early_exit": early_exit}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "early_exit_write_failed",
                    id=rec.get("id"),
                    horizon=horizon,
                    error=str(exc),
                )
    return triggered
