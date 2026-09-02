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

import re
from typing import cast

import structlog

from aistock_agent.services.cache import set_cached_validation_profile
from aistock_agent.services.data_client import node_api
from aistock_agent.services.prediction_stats import (
    baseline_neutral_summary,
    bucket_summary,
    build_validation_profile,
    hit_rate_summary,
)
from aistock_agent.services.prediction_targets import (
    INDEX_TARGETS,
    classify_target,
    resolve_sector_target,
)
from aistock_agent.services.prediction_targets import (
    resolve_index_or_stock_code as _resolve_index_or_stock,
)
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

# target（指数名）→ 6 位代码（G6 外置到 prediction_targets.py；别名兼容既有引用名）
_INDEX_CODE_MAP: dict[str, str] = INDEX_TARGETS

# neutral 方向判定阈值：涨跌幅绝对值低于该值视为横盘命中
_NEUTRAL_PCT_THRESHOLD = 0.5

# v2/v3 口径常量（H1/D1/D6/G13/G14；阶段 0 起 _METHODOLOGY_VERSION 为 3.0 窗口累计主判）
_WINDOW_DAYS_AFTER_DUE = 3      # 验证窗口 [due, due+3] 交易日
_METHODOLOGY_VERSION = "3.0"    # 验证器主链写入版本（3.0 窗口累计主判；H1 版本分桶）
# 存量回补目标版本：backfill 只回补 2.0 时代遗留 no_data，用 2.0 口径重验、写 2.0（不混版本）。
# 与 stats._CURRENT_METHODOLOGY_VERSION、Node publicRouter.CURRENT_METHODOLOGY_VERSION 同批切换。
_BACKFILL_METHODOLOGY_VERSION = "2.0"
_STRONG_PCT = 5.0              # grade strong_hit/strong_miss 幅度阈值
_KLINE_FETCH_DAYS = 200        # 区间拉取 days 上限（_fetch_kline_window index 分支）
# 区间拉取 days 上限（stock 端点校验 1-120；_fetch_kline_window stock 分支）
_STOCK_KLINE_FETCH_DAYS = 120

# Spec B §4.2：验证画像缓存 TTL（秒）——每日 16:00 run_once 更新，86400 次日失效重算
_PROFILE_CACHE_TTL = 86400

# H3：板块验证阈值（G0c 标定 neutral 0.25%/strong 3.0%，版本 1.0）；
# index 保持 0.5/5.0（_INDEX_THRESHOLDS 复用既有常量，_judge_window 默认参数行为不变）
SECTOR_THRESHOLDS: dict[str, float] = {"neutral_pct": 0.25, "strong_pct": 3.0}
_THRESHOLD_VERSION = "1.0"
_INDEX_THRESHOLDS: dict[str, float] = {
    "neutral_pct": _NEUTRAL_PCT_THRESHOLD,
    "strong_pct": _STRONG_PCT,
}


def _should_skip_horizon(entry: object) -> bool:
    """该档位是否已产出 result（hit/miss/insufficient）→ 到期验证应跳过。

    A1：early_exit-only 状态 dict（无 result，早退标记）不阻塞到期验证——
    early_exit 与最终结果分离存储，验证照常进行。
    """
    return isinstance(entry, dict) and "result" in entry


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
    """按 due 区间拉取日 K（统一 index/sector/stock）。返回升序 [{trade_date, pct_chg}]；
    pct_chg=None 行保留占位（H7，由调用方计数）。失败/空返回 None（=数据源故障）。"""
    rng = _range_around_due(due_date)
    if rng is None:
        # 脏 due_date 无法确定窗口 → 数据源故障语义（_verify_horizon 落 insufficient）
        return None
    start, end = rng
    if kind == "sector":
        raw = await node_api.get_ths_daily_range(code, start, end)
    elif kind == "stock":
        # Spec B：个股数据源接入（/internal/quote/{code}/kline，TushareKlineService），
        # 携带与指数一致的区间参数 [due-20, due+10]。
        raw = await node_api.get_stock_kline(
            code, _STOCK_KLINE_FETCH_DAYS, start_date=start, end_date=end)
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
            # Node 端 trade_date 为 Tushare 原始 YYYYMMDD，due_date 为 YYYY-MM-DD；
            # 统一归一化为 YYYY-MM-DD 才能精确匹配（幂等：已是该格式的行原样透传）。
            if re.fullmatch(r"\d{8}", d):
                d = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            parsed.append(
                {"trade_date": d, "pct_chg": pct if isinstance(pct, int | float) else None})
    parsed.sort(key=lambda x: str(x["trade_date"]))
    return parsed or None


def _judge_window(
    direction: str,
    window: list[float],
    neutral_pct: float = _NEUTRAL_PCT_THRESHOLD,
    strong_pct: float = _STRONG_PCT,
    methodology_version: str = _METHODOLOGY_VERSION,
) -> tuple[str, str | None]:
    """窗口主判（阶段 0 起默认 3.0 窗口累计口径）。返回 (result, grade)。

    - v2（"2.0"，存量回补口径）：bullish 任一日 >0；bearish 任一日 <0；neutral 任一日 |pct|<neutral_pct
    - v3（"3.0"，当前生产口径）：bullish 累计 sum>0；bearish 累计 sum<0；neutral mean(|p_i|)<neutral_pct

    grade 仅 bullish/bearish（G14）：strong_hit = due 当日命中 或 窗口内同向 |pct|>=strong_pct；
    strong_miss = 全反向 且 窗口内反向 |pct|>=strong_pct；否则 hit/miss。neutral 恒 None。

    H3：默认参数即 index 阈值（0.5/5.0），行为不变；sector 调用注入 0.25/3.0。
    """
    if methodology_version == "2.0":
        # v2：任一日符号命中（G13，无累计净值兜底）
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
    # v3：窗口累计主判（bullish sum>0 / bearish sum<0 / neutral mean(|p_i|)<thr）
    if direction == "bullish":
        if sum(window) <= 0:
            return "miss", ("strong_miss" if any(p <= -strong_pct for p in window) else "miss")
        strong = window[0] > 0 or any(p >= strong_pct for p in window)
        return "hit", ("strong_hit" if strong else "hit")
    if direction == "bearish":
        if sum(window) >= 0:
            return "miss", ("strong_miss" if any(p >= strong_pct for p in window) else "miss")
        strong = window[0] < 0 or any(p <= -strong_pct for p in window)
        return "hit", ("strong_hit" if strong else "hit")
    mean_abs = sum(abs(p) for p in window) / len(window)
    return ("hit" if mean_abs < neutral_pct else "miss"), None


async def _verify_horizon(
    record: dict[str, object],
    horizon: str,
    methodology_version: str = _METHODOLOGY_VERSION,
) -> dict[str, object]:
    """到期验证：取 [due, due+3] 窗口 kline，按版本口径主判（默认 3.0 窗口累计）。

    entry 新增 methodology_version（H1）、grade（仅 bullish/bearish，G14）、
    baseline_neutral（同窗口恒中性预测命中标记，供 baseline 对照，H6）、
    approximate（越年近似档结构化标记，统计剔除，H2）、
    target_type/matched_*（H8）、threshold_version（sector，H3）、prediction_id（H4）。
    methodology_version 参数：主链默认 _METHODOLOGY_VERSION（3.0）；backfill 传
    _BACKFILL_METHODOLOGY_VERSION（2.0）保持存量口径不混版本（阶段 0）。
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
    # Spec B/light_predict：index 别名/裸码/带后缀 ts_code/6 位个股裸码统一在此解析
    # （纯同步免网络），未命中才走板块 resolve（H3）。
    code, target_type = _resolve_index_or_stock(target)
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
        "methodology_version": methodology_version,
        "prediction_id": record.get("id"),  # H4 双计数关联
        "target_type": target_type,          # H8 目标类型（index/sector）
    }
    if code is None:
        kind = classify_target(target)
        src = {"sector": "未匹配板块名（resolve 未命中）",
               "stock": "个股代码无法解析（需 6 位代码或带后缀 ts_code）"}.get(
            kind, "抽象 target 漂移（LLM 输出质量问题）")
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
        strong_pct=float(thresholds["strong_pct"]),
        methodology_version=methodology_version)
    cumulative = sum(window)
    actual_str = f"{cumulative:+.2f}%"
    reason = f"方向={direction}, 窗口累计={actual_str}"
    if is_approximate:
        reason = f"(approximate_due_date) {reason}"
    # baseline_neutral（H6）随版本口径：v2 任一日 |p|<thr；v3 mean(|p_i|)<thr
    if methodology_version == "2.0":
        baseline_neutral = any(abs(p) < float(thresholds["neutral_pct"]) for p in window)
    else:
        baseline_neutral = (
            sum(abs(p) for p in window) / len(window) < float(thresholds["neutral_pct"])
        )
    out = {**base, "result": result, "actual": actual_str, "reason": reason,
           "approximate": is_approximate,  # H2 结构化标记（Task 4 统计过滤依据）
           "baseline_neutral": baseline_neutral}
    if target_type == "sector":
        out["threshold_version"] = _THRESHOLD_VERSION  # H3：sector 阈值版本（1.0）
    if grade is not None:
        out["grade"] = grade
    return out


# 带交易所后缀的指数 ts_code 消歧与 index/stock code 归一在 prediction_targets.py
# （resolve_index_or_stock_code，验证器/预判入口共用，本文件以 _resolve_index_or_stock 引用）。


async def _resolve_verify_target(
    target: str,
) -> tuple[str | None, str, dict[str, str] | None]:
    """index/sector/stock 目标资产解析（horizon 与 condition 共用）。

    返回 (code, target_type, matched)：index 直接命中代码映射/后缀 ts_code；6 位
    个股裸码或带后缀 ts_code → stock（Spec B：个股数据源已接入，不发网络请求）；
    否则尝试板块 resolve；均失败返回 (None, classify_target(target), None)。
    """
    code, target_type = _resolve_index_or_stock(target)
    if code is not None:
        return code, target_type, None
    resolved = await resolve_sector_target(target)
    if resolved:
        return str(resolved["ts_code"]), "sector", resolved
    return None, target_type, None


def _parse_threshold(value: str) -> float | None:
    """解析涨跌幅阈值（"+5%"→5.0、"-3%"→-3.0）；无效返回 None。"""
    if not isinstance(value, str):
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    return float(m.group(0)) if m else None


def _judge_condition_hit(
    direction: str, threshold_val: float | None, cumulative: float
) -> bool:
    """condition scenario 是否命中：按 anchor.direction + threshold 比对窗口累计。

    显式阈值（如 "+5%"/"-3%"）存在 → 与窗口累计累计直接比对（scenario 命中主判）；
    阈值缺省 → 退化为方向符号主判（bullish>0 / bearish<0 / neutral 横盘），
    对齐 _judge_window 语义。spec §9-5：条件成立两段判定推迟，此处仅起见
    scenario 命中与否。
    """
    if threshold_val is not None:
        if direction == "bullish":
            return cumulative >= max(threshold_val, 0.0)
        if direction == "bearish":
            return cumulative <= min(threshold_val, 0.0)
    if direction == "bullish":
        return cumulative > 0
    if direction == "bearish":
        return cumulative < 0
    return abs(cumulative) < _NEUTRAL_PCT_THRESHOLD


async def _verify_conditions(
    record: dict[str, object],
    methodology_version: str = _METHODOLOGY_VERSION,
) -> dict[str, object]:
    """条件化预判到期验证：对 conditions 的每条生成 c{i} entry（方案一，§4.2）。

    - 目标资产复用 record 的 horizons[0].target 解析（大盘/板块，§9-5 首批范围）；
    - condition_met 本批恒 null（两段判定推迟，§9-5 决策）；scenario 命中用
      anchor.direction + threshold 比对窗口累计；
    - entry 显式补 target_type（index/sector，§4.2/§11）避免统计漏桶；
    - 窗口未满 → {"wait": True}，run_once continue 不回写，下次补齐再验（D1 语义）；
    - 返回 {c{i}: entry}，run_once 对已存在 result 的 c{i} 幂等跳过。
    """
    prediction = record.get("prediction")
    if not isinstance(prediction, dict):
        return {}
    conditions = prediction.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return {}  # 2.0 旧记录/无条件预判无 c{i} 验证
    horizons = prediction.get("horizons")
    tgt = ""
    if isinstance(horizons, list) and horizons and isinstance(horizons[0], dict):
        tgt = str(horizons[0].get("target") or "")
    code, target_type, matched = await _resolve_verify_target(tgt)
    base: dict[str, object] = {
        "verified_at": shanghai_today().isoformat(),
        "methodology_version": methodology_version,
        "prediction_id": record.get("id"),
        "condition_met": None,  # 两段判定推迟（§9-5）
        "target_type": target_type,
    }
    if matched:
        base["matched_ts_code"] = str(matched["ts_code"])
        base["matched_name"] = str(matched["name"])
    due_dates = record.get("due_dates")
    due_dates_map = due_dates if isinstance(due_dates, dict) else {}
    out: dict[str, object] = {}
    for i, cond in enumerate(conditions):
        key = f"c{i}"
        if not isinstance(cond, dict):
            continue
        anchor = cond.get("anchor") if isinstance(cond.get("anchor"), dict) else {}
        horizon = anchor.get("horizon")
        due_date = str(due_dates_map.get(horizon) or "") if horizon else ""
        direction = str(anchor.get("direction") or "neutral")
        threshold = str(anchor.get("threshold") or "")
        entry: dict[str, object] = {
            **base,
            "condition_index": i,
            "horizon": horizon,
            "condition": cond.get("condition"),
            "scenario": cond.get("scenario"),
            "threshold": threshold,
        }
        if code is None:
            out[key] = {**entry, "result": "insufficient", "subtype": "no_source",
                        "actual": "", "reason": f"target '{tgt}' 无验证数据源"}
            continue
        if not due_date:
            out[key] = {**entry, "result": "insufficient", "subtype": "no_due_date",
                        "actual": "", "reason": "condition anchor 无对应 due_date"}
            continue
        rows = await _fetch_kline_window(target_type, code, due_date)
        if rows is None:
            out[key] = {**entry, "result": "insufficient", "subtype": "no_data",
                        "actual": "", "reason": "到期行情不可用"}
            continue
        missing = sum(1 for r in rows if r.get("pct_chg") is None)
        if missing > 0:
            out[key] = {**entry, "result": "insufficient", "subtype": "no_data",
                        "actual": "", "reason": f"行情数据缺失 {missing} 行"}
            continue
        idx = next((j for j, r in enumerate(rows) if r.get("trade_date") == due_date), None)
        if idx is None:
            out[key] = {**entry, "result": "insufficient", "subtype": "no_data",
                        "actual": "", "reason": f"到期日 {due_date} 行情缺失"}
            continue
        window = [float(cast(float, r["pct_chg"]))
                  for r in rows[idx: idx + _WINDOW_DAYS_AFTER_DUE + 1]]
        if len(window) < _WINDOW_DAYS_AFTER_DUE + 1:
            # D1：窗口未满不回写，下次 run_once 补齐再验
            wait_reason = (
                f"验证窗口未满（{len(window)}/{_WINDOW_DAYS_AFTER_DUE + 1}），等待补齐"
            )
            out[key] = {**entry, "wait": True, "reason": wait_reason}
            continue
        cumulative = sum(window)
        hit = _judge_condition_hit(direction, _parse_threshold(threshold), cumulative)
        out[key] = {
            **entry,
            "result": "hit" if hit else "miss",
            "actual": f"{cumulative:+.2f}%",
            "reason": f"direction={direction}, threshold={threshold or 'N/A'}, "
                      f"窗口累计={f'{cumulative:+.2f}%'}",
        }
    return out


async def backfill_no_data() -> int:
    """存量 no_data 回补：扫描 verified 记录中 _BACKFILL_METHODOLOGY_VERSION/no_data 的
    index 档位按区间重验（D4）。

    幂等：仅重验 entry 为 insufficient/no_data 的档位，hit/miss 不回补；sector 回补
    依赖 resolve，主链路已处理新记录，此处只回补 index。重验沿用存量版本口径
    （阶段 0：2.0 记录用 2.0 主判、写 2.0，不混入 3.0）。返回成功覆盖回写的档位数。
    """
    records = await node_api.list_verified_predictions(limit=500)
    updated = 0
    for record in records:
        verification = record.get("verification")
        if not isinstance(verification, dict):
            continue
        for horizon, entry in verification.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("methodology_version") != _BACKFILL_METHODOLOGY_VERSION:
                continue
            if entry.get("subtype") != "no_data" or entry.get("target_type") == "sector":
                continue  # sector 回补依赖 resolve，主链路已处理新记录；此处只回补 index
            if entry.get("result") != "insufficient":
                continue
            # 构造最小记录形状复用 _verify_horizon
            prediction = record.get("prediction")
            due_dates = record.get("due_dates")
            if not isinstance(prediction, dict) or not isinstance(due_dates, dict):
                continue
            re_entry = await _verify_horizon(
                {"id": record.get("id"), "prediction": prediction, "due_dates": due_dates},
                horizon,
                methodology_version=_BACKFILL_METHODOLOGY_VERSION,
            )
            if re_entry.get("wait") or re_entry.get("result") == "insufficient":
                continue  # 仍不可验则不覆盖
            try:
                await node_api.update_prediction_verification(int(record["id"]), horizon, re_entry)
                updated += 1
            except Exception as exc:
                logger.warning(
                    "prediction_backfill_failed",
                    id=record.get("id"),
                    horizon=horizon,
                    error=str(exc),
                )
    return updated


async def run_once() -> int:
    """扫描到期预测并回写验证结果。返回成功回写的档位数。"""
    today = shanghai_today()
    # D4 回补：存量 2.0/no_data 的 index 档按 due 区间重验。置于 pending 扫描之前，
    # 保证无 pending（早退路径）时回补仍执行；两批记录不相交，顺序无影响（resolution 1）。
    backfill_updated = await backfill_no_data()
    if backfill_updated:
        logger.info("prediction_backfill", count=backfill_updated)
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
            if due_date > today.isoformat() or _should_skip_horizon(verification.get(horizon)):
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
        # Spec A §4.2/§11：条件化预判双验证调度——3.0 记录对每条 condition 另产 c{i}
        # entry（c{i} key 与 horizon key 并存，A1 early_exit 不冲突）；已存在 result
        # 的 c{i} 幂等跳过。
        cond_entries = await _verify_conditions(record)
        for ckey, centry in cond_entries.items():
            if centry.get("wait"):
                continue  # D1：窗口未满不回写，下次补齐再验
            if _should_skip_horizon(verification.get(ckey)):
                continue  # 幂等：上一轮已产出 result 的 condition 跳过
            try:
                await node_api.update_prediction_verification(record_id, ckey, centry)
                updated += 1
                logger.info(
                    "prediction_condition_verified",
                    id=record_id,
                    key=ckey,
                    result=centry["result"],
                )
            except Exception as exc:
                logger.warning(
                    "prediction_condition_verify_write_failed",
                    id=record_id,
                    key=ckey,
                    error=str(exc),
                    exc_info=True,
                )
    # 日志输出（P0-2）
    if target_counter:
        logger.info("prediction_target_distribution", distribution=target_counter)
    # Spec B §4.2：到期验证接管——验证后按 target 落画像缓存（供预判 skill 读取 + 迭代闭环）
    try:
        await _write_validation_profiles()
    except Exception:  # noqa: BLE001
        logger.warning("prediction_profile_write_failed", exc_info=True)
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
    buckets = bucket_summary(entries)
    baseline = baseline_neutral_summary(entries)
    logger.info(
        "prediction_stats_summary",
        n=summary["n"],
        hit_rate=summary["hit_rate"],
        ci=summary["ci"],
        sufficient_sample=summary["sufficient_sample"],
        baseline_hit_rate=baseline["hit_rate"],
        buckets=buckets,
    )


async def _write_validation_profiles() -> int:
    """到期验证接管（Spec B §4.2）：验证后按 target 落画像缓存。

    读取 verified 窗口，把每条带 result 的 verification entry 归到 record 级 target
    字符串下，经 ``_resolve_verify_target`` 收敛为稳定 internal_id（stock/index=裸码，
    sector=ts_code；不直接用 name，防板块改名断画像），再 build_validation_profile +
    落 ``prediction:profile:{internal_id}`` 缓存——供预判 skill 读取 + 迭代闭环消费，
    避免每次预判拉全量 verified 重算（§8 拉取开销）。
    early_exit-only（无 result）不计入画像（§9-3）。返回写入的靶位数。
    """
    verified = await node_api.list_verified_predictions(limit=500)
    if not verified:
        return 0
    groups: dict[str, list[dict[str, object]]] = {}
    for rec in verified:
        tgt = _record_target_str(rec.get("prediction"))
        if tgt is None:
            continue
        ver = rec.get("verification")
        if not isinstance(ver, dict):
            continue
        for entry in ver.values():
            if isinstance(entry, dict) and "result" in entry:
                groups.setdefault(tgt, []).append(entry)
    if not groups:
        return 0
    written = 0
    for tgt, entries in groups.items():
        code, _, _ = await _resolve_verify_target(tgt)
        key = code or tgt
        profile = build_validation_profile(
            entries, key, methodology_version=_METHODOLOGY_VERSION)
        if await set_cached_validation_profile(key, profile, ttl=_PROFILE_CACHE_TTL):
            written += 1
        logger.info(
            "prediction_profile_written",
            target=key,
            n=profile["n"],
            hit_rate=profile["hit_rate"],
            degradation_rate=profile["degradation_rate"],
        )
    return written


def _record_target_str(prediction: object) -> str | None:
    """取 prediction 首个非空 target 字符串（画像分组用）。"""
    if not isinstance(prediction, dict):
        return None
    horizons = prediction.get("horizons")
    if isinstance(horizons, list):
        for h in horizons:
            if isinstance(h, dict) and h.get("target"):
                return str(h["target"])
    return None
