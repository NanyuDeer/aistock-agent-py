"""预测到期验证服务 — 收盘后扫描到期预测，对照实际行情判 hit/miss 并回写。

v1 对照口径：指数 target 用 /internal/index/quotes 当日涨跌幅符号 vs 预测方向；
非指数 target 数据源未接入 → 记为 insufficient（v1 限制，后续迭代扩展）。
"""

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

# target（指数名）→ /internal/index/quotes 的 6 位代码
_INDEX_CODE_MAP: dict[str, str] = {
    "上证指数": "000001",
    "上证": "000001",
    "深证成指": "399001",
    "深成指": "399001",
    "创业板指": "399006",
    "创业板": "399006",
    "科创50": "000688",
    "沪深300": "000300",
}

# neutral 方向判定阈值：涨跌幅绝对值低于该值视为横盘命中
_NEUTRAL_PCT_THRESHOLD = 0.5


def _extract_horizon_entry(prediction: object, horizon: str) -> dict[str, object] | None:
    """从 prediction jsonb 中取指定档位的 PredictionHorizon。"""
    if not isinstance(prediction, dict):
        return None
    horizons = prediction.get("horizons")
    if not isinstance(horizons, list):
        return None
    for h in horizons:
        if isinstance(h, dict) and h.get("horizon") == horizon:
            return h
    return None


async def _verify_horizon(record: dict[str, object], horizon: str) -> dict[str, object]:
    """对单档位做到期对照：resolve target → 取实际信号 → hit/miss/insufficient。"""
    prediction = record.get("prediction")
    entry = _extract_horizon_entry(prediction, horizon) or {}
    target = str(entry.get("target") or "")
    code = _INDEX_CODE_MAP.get(target)
    today = shanghai_today().isoformat()
    if code is None:
        return {
            "horizon": horizon,
            "result": "insufficient",
            "actual": "",
            "reason": f"target '{target}' 暂无验证数据源",
            "verified_at": today,
        }

    data = await node_api.get(f"/internal/index/quotes?symbols={code}")
    pct: float | None = None
    if isinstance(data, dict):
        indices = data.get("indices")
        if isinstance(indices, list):
            for idx in indices:
                if isinstance(idx, dict) and str(idx.get("index")) == code:
                    raw = idx.get("changePercent")
                    if isinstance(raw, int | float):
                        pct = float(raw)
                    break
    if pct is None:
        return {
            "horizon": horizon,
            "result": "insufficient",
            "actual": "",
            "reason": "指数行情不可用",
            "verified_at": today,
        }

    direction = str(entry.get("direction") or "neutral")
    actual_str = f"{pct:+.2f}%"
    if direction == "bullish":
        result = "hit" if pct > 0 else "miss"
    elif direction == "bearish":
        result = "hit" if pct < 0 else "miss"
    else:
        result = "hit" if abs(pct) < _NEUTRAL_PCT_THRESHOLD else "miss"
    return {
        "horizon": horizon,
        "result": result,
        "actual": actual_str,
        "reason": f"方向={direction}, 实际涨跌幅={actual_str}",
        "verified_at": today,
    }


async def run_once() -> int:
    """扫描到期预测并回写验证结果。返回成功回写的档位数。"""
    today = shanghai_today()
    records = await node_api.list_pending_predictions()
    if not records:
        logger.info("prediction_validate_no_pending")
        return 0
    updated = 0
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, int):
            continue
        due_dates = record.get("due_dates")
        verification = record.get("verification")
        if not isinstance(due_dates, dict) or not isinstance(verification, dict):
            continue
        for horizon, due_date in due_dates.items():
            if not (isinstance(horizon, str) and isinstance(due_date, str)):
                continue
            if due_date > today.isoformat() or horizon in verification:
                continue
            entry = await _verify_horizon(record, horizon)
            try:
                await node_api.update_prediction_verification(record_id, horizon, entry)
                updated += 1
                logger.info(
                    "prediction_verified",
                    id=record_id,
                    horizon=horizon,
                    result=entry["result"],
                )
            except Exception as exc:
                logger.warning(
                    "prediction_verify_write_failed",
                    id=record_id,
                    horizon=horizon,
                    error=str(exc),
                    exc_info=True,
                )
    return updated
