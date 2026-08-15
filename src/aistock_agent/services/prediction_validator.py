"""预测到期验证服务 — 收盘后扫描到期预测，对照实际行情判 hit/miss 并回写。

v2 对照口径（P0 预测验证升级）：
- 数据源：指数走 /internal/index/:code/kline（Tushare index_daily 历史日 K），
  不再用当日 /internal/index/quotes 快照。
- 判定：取 [due, due+3 交易日] 窗口日 K 涨跌幅符号命中主判（bullish 任一日>0→hit，
  bearish 任一日<0→hit，neutral 任一日 |pct|<0.5%→hit）；无累计净值兜底（G13：bullish/bearish
  下无符号命中日 ⇒ 累计必不命中，数学死代码）。
- grade：仅 bullish/bearish 计算（G14）；strong_hit=due 当日命中或窗口内同向 |pct|>=5%，
  strong_miss=全反向且窗口内反向 |pct|>=5%；neutral 恒不输出 grade。
- approximate 档（越年近似到期日）显式标记 approximate=True 不进主统计（H2）。
- 版本分桶：entry 带 methodology_version="2.0"（H1，与 schema_version 2.0 同步，D6）。
- 窗口未满（due+3 交易日尚未走完）→ 返回 {"wait": True}，run_once continue 不回写（D1）；
  数据源故障/到期日行情缺失 → 落 insufficient（可追溯，不混用 None 语义，D7）。
"""

from typing import cast

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.services.prediction_stats import baseline_neutral_summary, hit_rate_summary
from aistock_agent.services.prediction_targets import (
    INDEX_TARGETS,
    classify_target,
    resolve_sector_target,
)
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

# target（指数名）→ 6 位代码（G6 外置到 prediction_targets.py；别名兼容既有引用名）
_INDEX_CODE_MAP: dict[str, str] = INDEX_TARGETS

# neutral 方向判定阈值：涨跌幅绝对值低于该值视为横盘命中
_NEUTRAL_PCT_THRESHOLD = 0.5

# v2 口径常量（H1/D1/D6/G13/G14）
_WINDOW_DAYS_AFTER_DUE = 3      # v2 验证窗口 [due, due+3] 交易日
_METHODOLOGY_VERSION = "2.0"   # v2 口径版本（H1 版本分桶；与 schema_version 2.0 关联，D6）
_STRONG_PCT = 5.0              # grade strong_hit/strong_miss 幅度阈值
_KLINE_FETCH_DAYS = 200        # 区间拉取 days 上限（_fetch_kline_window index 分支）

# H3：板块验证阈值（G0c 标定 neutral 0.25%/strong 3.0%，版本 1.0）；
# index 保持 0.5/5.0（_INDEX_THRESHOLDS 复用既有常量，_judge_window 默认参数行为不变）
SECTOR_THRESHOLDS: dict[str, float] = {"neutral_pct": 0.25, "strong_pct": 3.0}
_THRESHOLD_VERSION = "1.0"
_INDEX_THRESHOLDS: dict[str, float] = {
    "neutral_pct": _NEUTRAL_PCT_THRESHOLD,
    "strong_pct": _STRONG_PCT,
}


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


def _range_around_due(due_date: str) -> tuple[str, str] | None:
    """due 前后缓冲窗口（起点=due-20 自然日，终点=due+10 自然日），YYYYMMDD。

    due_date 为 LLM 产出数据，脏值（空串/非 %Y-%m-%d）→ 返回 None，由调用方按
    数据源故障语义处理，不得让整批验证崩溃（旧 _fetch_index_window 不解析日期、
    天然安全失败，此守卫恢复该行为）。
    """
    from datetime import datetime, timedelta

    try:
        d = datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return None
    return ((d - timedelta(days=20)).strftime("%Y%m%d"),
            (d + timedelta(days=10)).strftime("%Y%m%d"))


async def _fetch_kline_window(
    kind: str, code: str, due_date: str
) -> list[dict[str, object]] | None:
    """按 due 区间拉取日 K（统一 index/sector）。返回升序 [{trade_date, pct_chg}]；
    pct_chg=None 行保留占位（H7，由调用方计数）。失败/空返回 None（=数据源故障）。"""
    rng = _range_around_due(due_date)
    if rng is None:
        # 脏 due_date 无法确定窗口 → 数据源故障语义（_verify_horizon 落 insufficient）
        return None
    start, end = rng
    if kind == "sector":
        raw = await node_api.get_ths_daily_range(code, start, end)
    else:
        raw = await node_api.get_index_kline(
            code, _KLINE_FETCH_DAYS, start_date=start, end_date=end)
    if not raw:
        return None
    parsed: list[dict[str, object]] = []
    for r in raw:
        d = r.get("trade_date")
        pct = r.get("pct_chg")
        if isinstance(d, str):
            parsed.append(
                {"trade_date": d, "pct_chg": pct if isinstance(pct, int | float) else None})
    parsed.sort(key=lambda x: str(x["trade_date"]))
    return parsed or None


def _judge_window(
    direction: str,
    window: list[float],
    neutral_pct: float = _NEUTRAL_PCT_THRESHOLD,
    strong_pct: float = _STRONG_PCT,
) -> tuple[str, str | None]:
    """符号命中主判（G13：无累计净值兜底）。返回 (result, grade)。

    - bullish: 任一日 >0 → hit；否则 miss
    - bearish: 任一日 <0 → hit；否则 miss
    - neutral: 任一日 |pct|<neutral_pct → hit；否则 miss
    grade 仅 bullish/bearish（G14）：strong_hit = due 当日命中 或 窗口内同向 |pct|>=strong_pct；
    strong_miss = 全反向 且 窗口内反向 |pct|>=strong_pct；否则 hit/miss。neutral 恒 None。

    H3：默认参数即 index 阈值（0.5/5.0），行为不变；sector 调用注入 0.25/3.0。
    """
    if direction == "bullish":
        if not any(p > 0 for p in window):
            return "miss", ("strong_miss" if any(p <= -strong_pct for p in window) else "miss")
        strong = window[0] > 0 or any(p >= strong_pct for p in window)
        return "hit", ("strong_hit" if strong else "hit")
    if direction == "bearish":
        if not any(p < 0 for p in window):
            return "miss", ("strong_miss" if any(p >= strong_pct for p in window) else "miss")
        strong = window[0] < 0 or any(p <= -strong_pct for p in window)
        return "hit", ("strong_hit" if strong else "hit")
    return ("hit" if any(abs(p) < neutral_pct for p in window) else "miss"), None


async def _verify_horizon(record: dict[str, object], horizon: str) -> dict[str, object]:
    """v2 到期验证：取 [due, due+3] 窗口 kline 符号命中主判。

    entry 新增 methodology_version（H1）、grade（仅 bullish/bearish，G14）、
    baseline_neutral（同窗口恒中性预测命中标记，供 baseline 对照，H6）、
    approximate（越年近似档结构化标记，统计剔除，H2）、
    target_type/matched_*（H8）、threshold_version（sector，H3）、prediction_id（H4）。
    返回语义（D1/D7）：正常 → hit/miss entry；窗口未满 → {"wait": True}（run_once 收到
    wait 则 continue 不回写，下次再验）；数据源故障/无数据 → insufficient entry（落库可追溯）。
    """
    prediction = record.get("prediction")
    entry = _extract_horizon_entry(prediction, horizon) or {}
    approx = prediction.get("due_dates_approximate") if isinstance(prediction, dict) else None
    is_approximate = isinstance(approx, list) and horizon in approx
    due_dates = record.get("due_dates")
    due_date = str(due_dates.get(horizon) or "") if isinstance(due_dates, dict) else ""
    target = str(entry.get("target") or "")
    code = _INDEX_CODE_MAP.get(target)
    target_type = "index"
    matched: dict[str, str] | None = None
    if code is None:
        # H3：指数未命中 → 尝试板块 resolve（三级匹配，Task 5 node_api.resolve_ths_name）
        resolved = await resolve_sector_target(target)
        if resolved:
            code = str(resolved["ts_code"])
            matched = resolved
            target_type = "sector"
    today = shanghai_today().isoformat()
    base: dict[str, object] = {
        "horizon": horizon,
        "verified_at": today,
        "methodology_version": _METHODOLOGY_VERSION,
        "prediction_id": record.get("id"),  # H4 双计数关联
        "target_type": target_type,          # H8 目标类型（index/sector）
    }
    if code is None:
        kind = classify_target(target)
        src = {"sector": "未匹配板块名（resolve 未命中）",
               "stock": "个股数据源（未接）"}.get(kind, "抽象 target 漂移（LLM 输出质量问题）")
        return {**base, "result": "insufficient", "subtype": "no_source", "actual": "",
                "reason": f"target '{target}' 无验证数据源：{src}"}
    if matched:
        base["matched_ts_code"] = str(matched["ts_code"])
        base["matched_name"] = str(matched["name"])
    rows = await _fetch_kline_window(target_type, code, due_date)
    if rows is None:
        # D7：数据源故障 ≠ 等窗口，必须落 insufficient（可追溯）
        return {**base, "result": "insufficient", "subtype": "no_data", "actual": "",
                "reason": "指数行情不可用"}
    # H7：缺值占位行计数，>0 落 insufficient 不静默
    missing = sum(1 for r in rows if r.get("pct_chg") is None)
    if missing > 0:
        return {**base, "result": "insufficient", "subtype": "no_data", "actual": "",
                "reason": f"行情数据缺失 {missing} 行（pct_chg 空）"}
    idx = next((i for i, r in enumerate(rows) if r.get("trade_date") == due_date), None)
    if idx is None and is_approximate:
        # G2 补丁：越年近似档（due 非真实交易日）→ 取 >= due 最近真实交易日兜底，
        # 消除 long 档系统性 no_data；approximate=True 标记由 H2 剔除主统计
        idx = next((i for i, r in enumerate(rows) if str(r.get("trade_date")) >= due_date), None)
        if idx is not None:
            base["due_matched"] = str(rows[idx]["trade_date"])
    if idx is None:
        return {**base, "result": "insufficient", "subtype": "no_data", "actual": "",
                "reason": f"到期日 {due_date} 行情缺失"}
    window = [float(cast(float, r["pct_chg"])) for r in rows[idx: idx + _WINDOW_DAYS_AFTER_DUE + 1]]
    # 注：_fetch_kline_window 已在上方过滤并计数 None 占位（H7）；此处 cast 规避 mypy object 类型
    if len(window) < _WINDOW_DAYS_AFTER_DUE + 1:
        # D1：窗口未满（due+3 尚未到）→ wait，run_once continue 不回写，下轮补齐再验
        reason = f"验证窗口未满（{len(window)}/{_WINDOW_DAYS_AFTER_DUE + 1}），等待补齐"
        if target_type == "sector":
            # H6：板块指数数据 N 交易日未更新 → reason 标注"板块指数数据可能停更"
            reason = f"{reason}（板块指数数据可能停更）"
        return {**base, "wait": True, "reason": reason}
    direction = str(entry.get("direction") or "neutral")
    # H3：sector 注入 0.25/3.0 阈值（G0c 标定），index 保持默认 0.5/5.0
    thresholds = SECTOR_THRESHOLDS if target_type == "sector" else _INDEX_THRESHOLDS
    result, grade = _judge_window(
        direction, window,
        neutral_pct=float(thresholds["neutral_pct"]),
        strong_pct=float(thresholds["strong_pct"]))
    cumulative = sum(window)
    actual_str = f"{cumulative:+.2f}%"
    reason = f"方向={direction}, 窗口累计={actual_str}"
    if is_approximate:
        reason = f"(approximate_due_date) {reason}"
    out = {**base, "result": result, "actual": actual_str, "reason": reason,
           "approximate": is_approximate,  # H2 结构化标记（Task 4 统计过滤依据）
           "baseline_neutral": any(abs(p) < float(thresholds["neutral_pct"]) for p in window)}
    if target_type == "sector":
        out["threshold_version"] = _THRESHOLD_VERSION  # H3：sector 阈值版本（1.0）
    if grade is not None:
        out["grade"] = grade
    return out


async def run_once() -> int:
    """扫描到期预测并回写验证结果。返回成功回写的档位数。"""
    today = shanghai_today()
    records: list[dict[str, object]] = []
    cursor: int | None = None
    while True:
        batch = await node_api.list_pending_predictions(limit=200, before_id=cursor)
        if not batch:
            break
        records.extend(batch)
        last_id = batch[-1].get("id")
        cursor = cast(int | None, last_id)
        if not isinstance(cursor, int) or len(batch) < 200:
            break
    if not records:
        logger.info("prediction_validate_no_pending")
        return 0
    updated = 0
    target_counter: dict[str, int] = {}
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
            # P0-2：target 漂移监控——对待验证档位统计 target 分类分布
            entry_h = _extract_horizon_entry(record.get("prediction"), horizon) or {}
            tgt = str(entry_h.get("target") or "?")
            kind = classify_target(tgt)
            target_counter[kind] = target_counter.get(kind, 0) + 1
            entry = await _verify_horizon(record, horizon)
            if entry.get("wait"):
                logger.info("prediction_validate_wait_window", id=record_id, horizon=horizon)
                continue  # 窗口未满：不回写，下次 run_once 补齐再验
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
    # 日志输出（P0-2）
    if target_counter:
        logger.info("prediction_target_distribution", distribution=target_counter)
    return updated


async def _report_stats() -> None:
    """验证统计出口：拉取 status=verified 记录 → hit_rate_summary/baseline_compare → 结构化日志。

    D3：verified 数据源用 Task 6 扩展的 listByStatus 游标（before_id 分页），
    不依赖 pending 游标设施；输出结构化日志供 P2 开 chat 对照与 B3 反哺做决策依据。
    """
    verified = await node_api.list_verified_predictions(limit=500)
    if not verified:
        return
    entries: list[dict[str, object]] = []
    for rec in verified:
        ver = rec.get("verification")
        if isinstance(ver, dict):
            for h, entry in ver.items():
                if isinstance(entry, dict):
                    entries.append(entry)
    if not entries:
        return
    summary = hit_rate_summary(entries)
    baseline = baseline_neutral_summary(entries)
    logger.info(
        "prediction_stats_summary",
        n=summary["n"],
        hit_rate=summary["hit_rate"],
        ci=summary["ci"],
        sufficient_sample=summary["sufficient_sample"],
        baseline_hit_rate=baseline["hit_rate"],
    )
