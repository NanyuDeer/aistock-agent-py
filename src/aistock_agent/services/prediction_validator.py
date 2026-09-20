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
- 条件化预判两段判定（Spec A §4.2；spec §12.5，2026-09-17 Task 6.1）：
  ① 到期前 `_scan_condition_met` 条件一成立即点亮 `verification[c{i}].condition_met=true`
  （确定性判定，无 result，**只写 true**）；
  ② 到期 `_verify_conditions` 照常写 hit/miss 并保留①已点亮的 true；**对确定性未成立的条件写
  `condition_met=false` + `checked_at`**（到期未成立态，与 true 对称的布尔；无法判定保持键缺失、
  绝不写 null；已点亮 true 显式防御不回退）。
- 条件类型分流（spec §12.3，2026-09-17 P4'）：`condition_met_judge.infer_condition_class` 按
  `anchor.metric/op/level/event_ref` + 条件文本确定性推断（事件类/量类/技术位/参考位/涨跌幅）；
  事件类三层（§12.4）：① 状态锚（Event Entity `event_status` ongoing/occurred → 确定性点亮）
  → ② 受限 LLM（`settings.condition_met_event_llm_enabled` 默认关，开启才调，带留痕）
  → ③ None 兜底；参考位类（today_open/high/low）取数层已透传 open/high/low（Task 10.1），
  以当日行（窗口最后一行）参考位与同行 close 判定，字段缺失时才降级 None。
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict

from aistock_agent.config import settings
from aistock_agent.services.cache import set_cached_validation_profile
from aistock_agent.services.condition_met_judge import (
    CONDITION_CLASS_EVENT,
    explain_unjudgeable_reason,
    infer_condition_class,
    judge_condition_met_state,
)
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
_KLINE_FETCH_DAYS = 200        # 区间拉取 days 上限（_fetch_kline_range index 分支）
# 区间拉取 days 上限（stock 端点校验 1-120；_fetch_kline_range stock 分支）
_STOCK_KLINE_FETCH_DAYS = 120

# Spec B §4.2：验证画像缓存 TTL（秒）——每日 16:00 run_once 更新，86400 次日失效重算
_PROFILE_CACHE_TTL = 86400

# condition_met 第①段扫描窗口（终审 #3 修正）：
#   窗口 = [created_at(开预判日), today]，**上限 120 自然日**（越界裁剪）；created_at 缺失 →
#   回退 today-120d；空窗（created_at 为未来脏值）→ 直接跳过不发请求。
#   旧实现误用 due 区间（_fetch_kline_window 的 [due-20, due+10]）：远端 due（long/越年档）
#   时该区间落在未来、对"今天"过滤后为空 → 静默跳过，长档条件几乎永不点亮。
_CONDITION_SCAN_MAX_DAYS = 120
# 判定只取窗口内最近 60 个交易日的尾部切片（D3：MA20/MA60 + 前低/新高所需形态）；
# 窗口本身是自然日区间，故取数长度由 _CONDITION_SCAN_MAX_DAYS 约束、此处只做尾部截取。
_CONDITION_SCAN_WINDOW = 60

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


def _num(v: object) -> float | None:
    """非数值（含 None/str/缺失）一律 None，保持缺值占位语义（H7）。"""
    return float(v) if isinstance(v, int | float) else None


def _today_ref_from_window(window: list[dict[str, object]]) -> dict[str, float] | None:
    """当日参考位（窗口最后一行的 close + open/high/low）——参考位类（today_open/high/low）判定输入。

    只取**同一行**：参考位（今日开/高/低）与比较基准 close 必须同源同行，否则逐维度剔 None 后
    跨行错位会误判（判定层 `_judge_ref_level_state` 以本次传入值同源为前提）。该行无 close、或三个
    参考位全缺（旧端点未透传 open/high/low）→ 返回 None（调用方降级不判）。
    """
    if not window:
        return None
    last = window[-1]
    if not isinstance(last, dict):
        return None
    close = last.get("close")
    if not isinstance(close, int | float):
        return None
    ref: dict[str, float] = {"close": float(close)}
    for key in ("open", "high", "low"):
        value = last.get(key)
        if isinstance(value, int | float):
            ref[key] = float(value)
    return ref if len(ref) > 1 else None


async def _fetch_kline_range(
    kind: str, code: str, start: str, end: str
) -> list[dict[str, object]] | None:
    """按**显式区间** [start, end]（YYYYMMDD）拉取日 K（统一 index/sector/stock）。

    返回升序 [{trade_date, pct_chg, close, vol}]；缺值行保留 None 占位（H7，由调用方计数）。
    失败/空返回 None（=数据源故障）。调用方决定区间口径（stage② due 区间 / stage① 扫描窗口）。
    """
    if kind == "sector":
        raw = await node_api.get_ths_daily_range(code, start, end)
    elif kind == "stock":
        # Spec B：个股数据源接入（/internal/quote/{code}/kline，TushareKlineService），
        # 携带与指数一致的区间参数。
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
        if isinstance(d, str):
            # Node 端 trade_date 为 Tushare 原始 YYYYMMDD，due_date 为 YYYY-MM-DD；
            # 统一归一化为 YYYY-MM-DD 才能精确匹配（幂等：已是该格式的行原样透传）。
            if re.fullmatch(r"\d{8}", d):
                d = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            # close/vol 供 condition_met 确定性判定（技术位/量类）使用；
            # amount 为 2026-09-17 Task 5.1 加性透传（index/stock 上游有值、sector 恒 null），
            # 供 metric=amount 的量类判定；本函数只做取数保留，不做判定。
            # open/high/low 为 2026-09-17 Task 10.1 加性透传（参考位类 today_open/high/low
            # 判定数据源；index/stock 上游已透传，sector 依赖 app-api ThsBoardDailyRow 同步扩展）。
            parsed.append({
                "trade_date": d,
                "pct_chg": _num(r.get("pct_chg")),
                "close": _num(r.get("close")),
                "vol": _num(r.get("vol")),
                "amount": _num(r.get("amount")),
                "open": _num(r.get("open")),
                "high": _num(r.get("high")),
                "low": _num(r.get("low")),
            })
    parsed.sort(key=lambda x: str(x["trade_date"]))
    return parsed or None


async def _fetch_kline_window(
    kind: str, code: str, due_date: str
) -> list[dict[str, object]] | None:
    """按 due 区间（[due-20, due+10] 自然日）拉取日 K —— **stage②（horizon/到期 condition）
    专用窗口，语义不变**；stage① 扫描另走 `_condition_scan_range` + `_fetch_kline_range`。"""
    rng = _range_around_due(due_date)
    if rng is None:
        # 脏 due_date 无法确定窗口 → 数据源故障语义（_verify_horizon 落 insufficient）
        return None
    return await _fetch_kline_range(kind, code, rng[0], rng[1])


def _condition_scan_range(record: dict[str, object], today: str) -> tuple[str, str] | None:
    """第①段（到期前扫描）取数窗口 [created_at, today]（YYYYMMDD）；空窗返回 None。

    终审 #3 裁决口径：
    - 起点 = record.created_at（Node 端 `prediction_records.created_at`）的日期部分；
      缺失/脏值 → 回退 `today - _CONDITION_SCAN_MAX_DAYS` 自然日；
    - 上限 `_CONDITION_SCAN_MAX_DAYS` 自然日：早于 `today - 120d` 的起点裁剪到该下限；
    - 裁剪后 start > end（created_at 为未来脏值）→ 返回 None，调用方**直接跳过、不发请求**。
    """
    from datetime import datetime, timedelta

    try:
        end_d = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return None
    floor_d = end_d - timedelta(days=_CONDITION_SCAN_MAX_DAYS)
    created = record.get("created_at")
    start_d = floor_d
    if isinstance(created, str) and len(created) >= 10:
        try:
            start_d = max(datetime.strptime(created[:10], "%Y-%m-%d").date(), floor_d)
        except ValueError:
            start_d = floor_d  # 脏 created_at → 回退 120 日
    if start_d > end_d:
        return None
    return start_d.strftime("%Y%m%d"), end_d.strftime("%Y%m%d")


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
        # D6（2026-09-03）：到期日当天（含盘中/收盘前）日 K 通常尚未入库 → 判 wait 而非
        # insufficient——盘中跑若落 insufficient 会被 _should_skip_horizon 拦下，16:00 收盘后
        # 无法重判，档位被永久写死 no_data。仅当 due 已过且行情缺失（停牌/数据故障）才落
        # insufficient 可追溯。
        if due_date >= today:
            return {**base, "wait": True,
                    "reason": f"到期日 {due_date} 当日行情未出，等待收盘后判定"}
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
    *,
    scan_cache: dict[tuple[str, str, str, str], list[dict[str, object]] | None] | None = None,
    event_cache: dict[tuple[str, str], dict[str, dict[str, object]]] | None = None,
) -> dict[str, object]:
    """条件化预判到期验证：对 conditions 的每条生成 c{i} entry（方案一，§4.2）。

    - 目标资产复用 record 的 horizons[0].target 解析（大盘/板块，§9-5 首批范围）；
    - 到期未成立态（spec §12.5，Task 6.1）：`result` 落库那一刻按第①段**同一判定能力**对
      确定性未成立的条件写 `condition_met=false` + `checked_at`（窗口 = [created_at, due]）；
      已点亮 `true` 的 entry 显式防御、不得回退为 false；无法判定 → 不写该键（绝不写 null）；
    - scenario 命中用 anchor.direction + threshold 比对窗口累计；
    - entry 显式补 target_type（index/sector，§4.2/§11）避免统计漏桶；
    - 窗口未满 → {"wait": True}，run_once continue 不回写，下次补齐再验（D1 语义）；
    - 返回 {c{i}: entry}，run_once 对已存在 result 的 c{i} 幂等跳过。
    - `scan_cache`/`event_cache`：与第①段共用的取数/事件记忆化（key 含窗口，不串用）。
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
        "target_type": target_type,
    }
    if matched:
        base["matched_ts_code"] = str(matched["ts_code"])
        base["matched_name"] = str(matched["name"])
    due_dates = record.get("due_dates")
    due_dates_map = due_dates if isinstance(due_dates, dict) else {}
    verification = record.get("verification")
    ver_map = verification if isinstance(verification, dict) else {}
    out: dict[str, object] = {}
    today = shanghai_today().isoformat()
    for i, cond in enumerate(conditions):
        key = f"c{i}"
        if not isinstance(cond, dict):
            continue
        anchor = cond.get("anchor") if isinstance(cond.get("anchor"), dict) else {}
        horizon = anchor.get("horizon")
        due_date = str(due_dates_map.get(horizon) or "") if horizon else ""
        direction = str(anchor.get("direction") or "neutral")
        threshold = str(anchor.get("threshold") or "")
        # D6（2026-09-03）：条件到期日仍在未来 → 未到验证窗口，跳过不产 entry（run_once 会在
        # 到期后自然处理）；此前对未来 due 落 insufficient no_data 违反窗口语义。
        if due_date and due_date > today:
            continue
        entry: dict[str, object] = {
            **base,
            "condition_index": i,
            "horizon": horizon,
            "condition": cond.get("condition"),
            "scenario": cond.get("scenario"),
            "threshold": threshold,
        }
        # 第①段（_scan_condition_met）已点亮的 condition_met=true 必须原样带出——Node 端
        # verification[c{i}] 为键级浅合并，不写该键即保留旧值，但显式写 null 会抹掉点亮。
        existing = ver_map.get(key)
        if isinstance(existing, dict) and existing.get("condition_met") is True:
            entry["condition_met"] = True
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
            # D6：到期日当天日 K 未出（盘中/收盘前）→ wait 待收盘后判定，不落 insufficient
            if due_date >= today:
                out[key] = {**entry, "wait": True,
                            "reason": f"到期日 {due_date} 当日行情未出，等待收盘后判定"}
                continue
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
    # 到期末成立态（spec §12.5，Task 6.1）：result 落库那一刻对**未触发**条件写
    # `condition_met=false` + `checked_at`——与第①段的 true 形成完整布尔，前端"到期未触发"
    # 与"在途未触发"从此可区分（此前只能以卡级 verification 近似）。三条硬约束：
    #   ① **不得回退**：已点亮 true 的条件不得改写成 false（jsonb 键级浅合并天然保留，
    #      此处显式防御）；
    #   ② **不写 null**：无法判定（参考位降级/无 level 量类/无数据/无行情源）→ 不写该键，
    #      保持缺失（前端按"未触发/在途"处理）；
    #   ③ **只在此刻写**：wait（窗口未满）分支不产 result 故不写；已含 result 的 c{i} 由
    #      run_once 的幂等守卫跳过，重复扫描不产生重复副作用。
    cache_scan = scan_cache if scan_cache is not None else {}
    cache_events = event_cache if event_cache is not None else {}
    for key, entry in out.items():
        if "result" not in entry or entry.get("condition_met") is True:
            continue  # 未到期末判定 / 已点亮 true（第①段点亮或上方带出）
        existing = ver_map.get(key)
        if isinstance(existing, dict) and "result" in existing:
            # 该 c{i} 已到期末判定（run_once 会幂等跳过回写）→ 不再重复判定/取数
            continue
        idx = entry.get("condition_index")
        if not isinstance(idx, int) or not 0 <= idx < len(conditions):
            continue
        cond = conditions[idx]
        if not isinstance(cond, dict):
            continue
        horizon = entry.get("horizon")
        cond_due = str(due_dates_map.get(str(horizon)) or "") if horizon else ""
        met = await _judge_condition_met_once(
            cond,
            target_type=target_type,
            code=code,
            # 到期判定窗口 = [created_at, due]（第①段是 [created_at, today]）
            window_range=_condition_scan_range(record, cond_due) if cond_due else None,
            scan_cache=cache_scan,
            event_cache=cache_events,
            at_due=True,
        )
        if met is None:
            continue  # 无法判定 → 保持键缺失
        entry["condition_met"] = met
        entry["checked_at"] = today
    return out


# ── 事件类条件三层判定（spec §12.4，Task 5.1）──
# ① 状态锚（确定性）：Event Entity `event_status ∈ {ongoing, occurred}` → 条件成立；
# ② 受限 LLM（`settings.condition_met_event_llm_enabled`，**默认关闭**）：仅事件类条件、
#    输入限定"事件标题 + 进展摘要 + 条件文本"、输出 true/false/unknown + 置信，低置信归
#    unjudgeable；每次判定留痕（prompt 版本 / 事件 id / 结论 / 置信）。
# ③ 兜底 None（无法判定，前端显示"无法判定"，不等同"未触发"）。
_EVENT_MET_STATUSES = frozenset({"ongoing", "occurred"})
# 已物化但未落地（尚未发生）→ 条件不成立，无需进 ② 层（确定性短路）
_EVENT_OPEN_STATUSES = frozenset({"scheduled", "upcoming"})
_EVENT_LLM_PROMPT_VERSION = "event-condition-v1"
# 最低可采信置信度：low 置信一律归 None（spec §12.4 ②）
_EVENT_LLM_MIN_CONFIDENCE = frozenset({"high", "medium"})


class _EventConditionVerdict(BaseModel):
    """受限 LLM 输出契约（spec §12.4 ②）：三值结论 + 置信度（禁止多余键）。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["true", "false", "unknown"]
    confidence: Literal["high", "medium", "low"]


_EVENT_CONDITION_SYSTEM_PROMPT = """你是 A 股事件类预判条件的成立判定器。
输入只有三样：事件标题、事件进展摘要、条件文本。判定该条件**当前是否已成立**。
只依据输入内容判断；信息不足、事件未落地或无法从摘要确认时，结论必须用 unknown。
只输出 JSON：{"verdict": "true"|"false"|"unknown", "confidence": "high"|"medium"|"low"}
（verdict=true 表示条件已成立；false 表示条件已确定不成立；unknown 表示信息不足）。
不要输出解释、Markdown 或其他键。"""


def _yyyymmdd_to_iso(value: str) -> str:
    """YYYYMMDD → YYYY-MM-DD（Event Entity 读接口的日期参数格式）；脏值原样返回。"""
    if len(value) == 8 and value.isdigit():
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


async def _load_event_index(
    date_from: str, date_to: str, *, cache: dict[tuple[str, str], dict[str, dict[str, object]]]
) -> dict[str, dict[str, object]]:
    """读 Event Entity 列表 → {event_id: 事件条目}（条目含 event_status/title/summary）。

    `date_from`/`date_to` 为扫描窗口（YYYYMMDD），转 ISO 后按 Event Entity 的
    `dateFrom`/`dateTo` 过滤（作用于 `event_start_time` 的上海日期）。读取失败/异常 → 空索引
    （**fail-safe：不点亮**，交 ②/③ 层）。同一次 run_once 内按 (dateFrom, dateTo) 记忆化。
    """
    iso_from = _yyyymmdd_to_iso(date_from)
    iso_to = _yyyymmdd_to_iso(date_to)
    key = (iso_from, iso_to)
    if key in cache:
        return cache[key]
    index: dict[str, dict[str, object]] = {}
    try:
        items = await node_api.get_event_entities({"dateFrom": iso_from, "dateTo": iso_to})
    except Exception as exc:  # noqa: BLE001 —— 读失败等价"未物化"，不得点亮
        logger.warning("condition_met_event_read_failed", error=str(exc))
        items = None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            event_id = item.get("event_id")
            if isinstance(event_id, str) and event_id.strip():
                index[event_id] = item
    cache[key] = index
    return index


async def _judge_event_condition(
    event_ref: str, condition_text: str, event: dict[str, object] | None,
    *, at_due: bool = False,
) -> bool | None:
    """事件类条件三层判定（spec §12.4）：① 状态锚 → ② 受限 LLM → ③ None。

    - 事件未读到（缺失/未物化/读失败）→ ③ None（无标题/摘要输入，不空跑 LLM）；
    - 状态 ∈ {ongoing, occurred} → ① `True`（确定性，不调 LLM）；
    - 状态 ∈ {scheduled, upcoming} → **到期时**（`at_due=True`）为确定性未成立 `False`
      （Task 6.1：到期未触发条件要写 `condition_met=false`）；扫描期（未到期）返回 None
      ——第①段"只写 true"，不写 false；
    - 其余（状态缺失/未知）→ ② 受限 LLM（开关默认关，关闭时直落 ③）。
    """
    if event is None:
        return None  # ③ 无事件可锚（不编造、不空跑 LLM）
    status = str(event.get("event_status") or "").strip()
    if status in _EVENT_MET_STATUSES:
        return True  # ① 状态锚：确定性点亮
    if status in _EVENT_OPEN_STATUSES:
        return False if at_due else None  # 未落地 → 未到期不写 false，到期为确定性未成立
    return await _judge_event_condition_llm(event_ref, condition_text, event)  # ② → ③


async def _judge_event_condition_llm(
    event_ref: str, condition_text: str, event: dict[str, object]
) -> bool | None:
    """事件类 ② 层：受限 LLM 判定（仅事件类；输入限定标题+摘要+条件文本）。

    治理与成本（Task 5.1 裁决）：`settings.condition_met_event_llm_enabled` **默认 False**——
    条件点亮是"只写 true 不可撤回"的写入，LLM 半确定性结论在大范围灰度前不放开；开关打开后
    每次判定写 `condition_met_event_llm_judged` 留痕（prompt 版本 / 事件 id / 结论 / 置信），
    低置信（low）与解析失败一律归 None（unjudgeable）。
    """
    if not settings.condition_met_event_llm_enabled:
        return None  # ② 层默认关闭 → ③ 兜底
    from aistock_agent.services.llm import get_quick_think, with_chat_structured_output

    payload = {
        "event_id": event_ref,
        "event_title": str(event.get("title") or ""),
        "event_summary": str(event.get("summary") or ""),
        "condition": condition_text,
    }
    try:
        llm = with_chat_structured_output(get_quick_think(), _EventConditionVerdict)
        verdict = await llm.ainvoke([
            {"role": "system", "content": _EVENT_CONDITION_SYSTEM_PROMPT},
            {"role": "user", "content": str(payload)},
        ])
    except Exception as exc:  # noqa: BLE001 —— LLM 失败不得点亮
        logger.warning(
            "condition_met_event_llm_failed",
            event_ref=event_ref,
            prompt_version=_EVENT_LLM_PROMPT_VERSION,
            error=str(exc),
        )
        return None
    if verdict is None:
        logger.warning(
            "condition_met_event_llm_empty",
            event_ref=event_ref,
            prompt_version=_EVENT_LLM_PROMPT_VERSION,
        )
        return None
    conclusion = str(getattr(verdict, "verdict", "unknown"))
    confidence = str(getattr(verdict, "confidence", "low"))
    # 留痕（spec §12.4 ②）：prompt 版本 / 事件 id / 结论 / 置信
    logger.info(
        "condition_met_event_llm_judged",
        event_ref=event_ref,
        prompt_version=_EVENT_LLM_PROMPT_VERSION,
        verdict=conclusion,
        confidence=confidence,
    )
    if conclusion == "true" and confidence in _EVENT_LLM_MIN_CONFIDENCE:
        return True
    return None


async def _judge_condition_met_once(
    cond: dict[str, object],
    *,
    target_type: str,
    code: str | None,
    window_range: tuple[str, str] | None,
    scan_cache: dict[tuple[str, str, str, str], list[dict[str, object]] | None],
    event_cache: dict[tuple[str, str], dict[str, dict[str, object]]],
    at_due: bool = False,
) -> bool | None:
    """单条条件的成立判定（第①段扫描与到期未成立态**共用**；spec §12.3/§12.4，Task 6.1）。

    返回三值：`True`=成立 / `False`=**确定性不成立** / `None`=无法判定（不得写键）。

    为什么抽成一处：到期写 `condition_met=false` 必须与第①段点亮 true **同一判定能力**
    （同分流、同守卫、同取数口径），否则会出现"点亮与置否两套口径"（spec §12.3-3 同源维护）。

    - 窗口由调用方给定：第①段 = `[created_at, today]`（只判 true）；到期 = `[created_at, due]`；
    - 事件类 → 三层判定（`at_due=True` 时 `scheduled/upcoming` 为确定性未成立 False）；
    - 行情/量/技术位类 → `judge_condition_met_state`（确定性，禁 LLM）；
    - `code is None`（无行情数据源）/ 空窗口 / 无数据 → `None`（不判定）。
    """
    anchor_raw = cond.get("anchor")
    anchor: dict[str, object] = (
        cast(dict[str, object], anchor_raw) if isinstance(anchor_raw, dict) else {}
    )
    condition_text = str(cond.get("condition") or "")
    metric = str(anchor.get("metric") or "") or None
    op = str(anchor.get("op") or "") or None
    level = _num(anchor.get("level"))
    event_ref = str(anchor.get("event_ref") or "").strip() or None
    condition_class = infer_condition_class(
        metric=metric, event_ref=event_ref, text=condition_text
    )
    if condition_class == CONDITION_CLASS_EVENT:
        # 事件类（§12.4）：不需要行情；三层判定 + 事件列表记忆化
        index = (
            await _load_event_index(*window_range, cache=event_cache)
            if window_range is not None
            else {}
        )
        return await _judge_event_condition(
            event_ref or "", condition_text, index.get(event_ref or ""), at_due=at_due
        )
    if code is None or window_range is None:
        return None  # 无行情数据源/空窗口：行情/量类条件无法判（交第②段落 insufficient）
    cache_key = (target_type, code, window_range[0], window_range[1])
    if cache_key in scan_cache:
        rows = scan_cache[cache_key]  # 记忆化：同窗口（同记录多 condition）只取一次数
    else:
        rows = await _fetch_kline_range(target_type, code, *window_range)
        scan_cache[cache_key] = rows
    if not rows:
        return None  # 数据源故障/无数据：静默跳过（不产 false 键）
    window = rows[-_CONDITION_SCAN_WINDOW:]
    closes = [float(cast(float, r["close"])) for r in window if r.get("close") is not None]
    pct_chgs = [
        float(cast(float, r["pct_chg"])) for r in window if r.get("pct_chg") is not None
    ]
    volumes = [float(cast(float, r["vol"])) for r in window if r.get("vol") is not None]
    amounts = [
        float(cast(float, r["amount"])) for r in window if r.get("amount") is not None
    ]
    # 参考位类（today_open/high/low）判定输入：当日行（窗口最后一行）的开/高/低 + 同行 close
    today_ref = _today_ref_from_window(window)
    return judge_condition_met_state(
        condition_text,
        direction=str(anchor.get("direction") or "neutral"),
        threshold_pct=_parse_threshold(str(anchor.get("threshold") or "")),
        closes=closes,
        pct_chgs=pct_chgs,
        volumes=volumes,
        amounts=amounts,
        metric=metric,
        op=op,
        level=level,
        event_ref=event_ref,
        today_ref=today_ref,
    )


async def _scan_condition_met(
    record: dict[str, object],
    methodology_version: str = _METHODOLOGY_VERSION,
    *,
    scan_cache: dict[tuple[str, str, str, str], list[dict[str, object]] | None] | None = None,
    event_cache: dict[tuple[str, str], dict[str, dict[str, object]]] | None = None,
) -> dict[str, dict[str, object]]:
    """条件化预判第①段：到期前条件扫描（只点亮 `condition_met=true`，§4.2）。

    与 _verify_conditions（第②段·到期 hit/miss）同一 16:00 任务内执行（D4）；逐条 condition：

    - 已有 `condition_met is True` → 跳过（幂等：不重复点亮）；
    - 已有 `result` → 跳过（已到期末判定，不得覆盖）；
    - `due_date <= today` → 跳过，交由 _verify_conditions 处理；
    - 否则按 `infer_condition_class` 分流（spec §12.3，Task 5.1）：
      · 事件类 → 三层判定（状态锚 → 受限 LLM（默认关）→ None），**不拉行情**；
      · 其余 → 拉扫描窗口行情（终审 #3：**窗口 = [created_at, today]，上限 120 自然日**，
        见 `_condition_scan_range`；旧实现误用 due 区间导致远端 due 恒空窗）→ 组装
        closes/pct_chgs/volumes/amounts（各自剔除 None）→ `judge_condition_met_state`
        （确定性，禁 LLM）。
    目标资产无法解析（无行情数据源）时只跳过行情类条件，事件类条件仍按状态锚判定。

    判定为 True 才产 entry（`{condition_index, horizon(anchor 档位，D5), condition, scenario,
    threshold, condition_met: True, ...base}`，**不含 result**）；不成立/无法判定（含参考位降级、
    无 level 的量类）不产 entry —— 只写 true 不写 false（D1）。

    `scan_cache`：同一次 run_once 内 stage① 取数记忆化（key=(target_type, code, start, end)，
    含窗口以防跨记录串用——不同 created_at 的窗口不同）。传 None 时仅在本记录内生效。
    `event_cache`：同一次 run_once 内 Event Entity 列表记忆化（key=(dateFrom, dateTo)，
    避免每条事件类条件重复拉全表）；传 None 时仅在本记录内生效。
    """
    prediction = record.get("prediction")
    if not isinstance(prediction, dict):
        return {}
    conditions = prediction.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return {}
    today = shanghai_today().isoformat()
    scan_range = _condition_scan_range(record, today)
    if scan_range is None:
        return {}  # 空窗：不产 entry 且不发请求（避免必然空请求）
    horizons = prediction.get("horizons")
    tgt = ""
    if isinstance(horizons, list) and horizons and isinstance(horizons[0], dict):
        tgt = str(horizons[0].get("target") or "")
    code, target_type, matched = await _resolve_verify_target(tgt)
    # 目标资产无法解析（无行情数据源）时**不提前 return**：事件类条件不依赖 kline（只读事件
    # status），提前 return 会让可判定的事件条件被无关的行情解析失败连带跳过；行情类条件在
    # 下方逐条 `continue`（本段不产点亮 entry，交 _verify_conditions 第②段落 insufficient）。
    cache = scan_cache if scan_cache is not None else {}
    events = event_cache if event_cache is not None else {}
    base: dict[str, object] = {
        "verified_at": today,
        "methodology_version": methodology_version,
        "prediction_id": record.get("id"),
        "target_type": target_type,
    }
    if matched:
        base["matched_ts_code"] = str(matched["ts_code"])
        base["matched_name"] = str(matched["name"])
    due_dates = record.get("due_dates")
    due_dates_map = due_dates if isinstance(due_dates, dict) else {}
    verification = record.get("verification")
    ver_map = verification if isinstance(verification, dict) else {}
    out: dict[str, dict[str, object]] = {}
    # 2026-09-19 审计（组长裁定方案 A 的观测项）：按记录聚合"未点亮"归因码，落一条日志，
    # 用于回答"条件为什么不亮"（四道护栏 vs 数据缺失 vs 确定性不成立）。不改判定行为。
    unlit_reasons: dict[str, int] = {}
    checked = 0
    for i, cond in enumerate(conditions):
        key = f"c{i}"
        if not isinstance(cond, dict):
            continue
        existing = ver_map.get(key)
        if isinstance(existing, dict):
            if existing.get("condition_met") is True:
                continue  # 幂等：已点亮不重复写
            if "result" in existing:
                continue  # 已到期末判定，不覆盖
        anchor_raw = cond.get("anchor")
        anchor: dict[str, object] = (
            cast(dict[str, object], anchor_raw) if isinstance(anchor_raw, dict) else {}
        )
        horizon = anchor.get("horizon")
        due_date = str(due_dates_map.get(str(horizon)) or "") if horizon else ""
        if not due_date or due_date <= today:
            continue  # 已到期/无 due → 交 _verify_conditions 第②段
        # 判定与到期未成立态**共用**（Task 6.1）：本段只取 True 点亮，False/None 一律不产键。
        checked += 1
        met = await _judge_condition_met_once(
            cond,
            target_type=target_type,
            code=code,
            window_range=scan_range,
            scan_cache=cache,
            event_cache=events,
        )
        if met is not True:
            # 归因码（纯诊断）：False=确定性不成立；None=判不出 → 交 explain 函数细分类
            reason = (
                "deterministic_false"
                if met is False
                else explain_unjudgeable_reason(
                    str(cond.get("condition") or ""),
                    metric=str(anchor.get("metric") or "") or None,
                    event_ref=str(anchor.get("event_ref") or "") or None,
                )
            )
            unlit_reasons[reason] = unlit_reasons.get(reason, 0) + 1
            continue  # 不成立/无法判定 → 不产键（只写 true，D1）
        out[key] = {
            **base,
            "condition_index": i,
            "horizon": horizon,  # D5：anchor 档位（data_client 以 anchor_horizon 透传）
            "condition": cond.get("condition"),
            "scenario": cond.get("scenario"),
            "threshold": str(anchor.get("threshold") or ""),
            "condition_met": True,
        }
    if checked:
        logger.info(
            "prediction_condition_met_unlit_reasons",
            id=record.get("id"),
            checked=checked,
            lit=len(out),
            reasons=unlit_reasons,
        )
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


# ── 存量条件回溯补算（spec §12.6；计划 Task 6.2）──
# 为什么需要：`run_once` 只扫 `status='pending'`（+ 2.0/no_data 回补），已 `verified` 记录不再被扫
# → 存量记录的条件永远无布尔 `condition_met`（实测 88 个 entry 全无）。本入口一次性补齐。
_BACKFILL_CONDITION_SCHEMA_VERSION = "3.0"  # 只扫 3.0（conditions 契约）


@dataclass(frozen=True)
class BackfillConditionMetStats:
    """`backfill_condition_met` 统计出口（dry-run 报告 + 人工确认依据）。"""

    scanned: int = 0             # 扫描记录数（list_all_predictions 全量）
    candidates: int = 0          # 命中目标集合的记录数（3.0 + 含 conditions + 有无布尔 c{i}）
    judgeable: int = 0           # 可判定条件数（= lit + unmet）
    lit: int = 0                 # 判定成立（补写 true）
    unmet: int = 0               # 判定确定性不成立（补写 false，仅已到期）
    unjudgeable: int = 0         # 无法判定（不写键）
    skipped_in_flight: int = 0   # 未到期且判定不成立 → 不写（到期前只写 true，§12.5）
    written: int = 0             # 实际写入键数（dry_run 恒 0）
    write_failed: int = 0        # 写失败数（不炸整批）
    samples: tuple[str, ...] = ()  # 抽样明细（人工复核；上限 sample_size）


def _condition_met_payload(
    existing: dict[str, object] | None,
    index: int,
    horizon: object,
    met: bool,
    checked_at: str,
) -> dict[str, object]:
    """构造"只补 condition_met/checked_at"的回写 payload（Task 6.2）。

    为什么把既有 entry 整条回传：Node 侧 `verification[c{i}]` 是**键级浅合并**，且会为每条
    非 early_exit entry 补 `actual:''/reason:''/verified_at:now` 默认值——只发 condition_met
    会把已判档位的 actual/reason/verified_at 覆盖成默认值（审计信息损失）。整条回传后这些键
    逐字节不变，真正变化的只有 `condition_met`/`checked_at`（+ 定位键 condition_index/horizon）。
    """
    payload: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    payload.pop("type", None)        # 早退标记 entry 不属条件档位（防御，正常不存在）
    payload.pop("early_exit", None)
    payload["condition_index"] = index
    if isinstance(horizon, str) and horizon:
        # D5 口径：data_client 见 entry.horizon != body.horizon → 以 anchor_horizon 透传
        payload["horizon"] = horizon
    payload["condition_met"] = met
    payload["checked_at"] = checked_at
    return payload


async def backfill_condition_met(
    *,
    dry_run: bool = True,
    limit: int = 200,
    batch_size: int = 20,
    sleep_seconds: float = 0.5,
    max_records: int | None = None,
    sample_size: int = 10,
) -> BackfillConditionMetStats:
    """存量条件回溯补算（spec §12.6，Task 6.2）——独立入口、可重入、限速，**默认 dry-run**。

    目标集合：`schema_version='3.0'` + `prediction.conditions[]` 非空 + 对应
    `verification[c{i}]` **尚无布尔 `condition_met`** 的记录。
    动作：按 Phase 5 判定能力（`_judge_condition_met_once`，与第①/②段同源）**只补写
    `condition_met`（+ `checked_at`）**，**不覆盖** `result`/`window` 等其他键；不写 `null`。

    口径（与主链路一致，见 spec §12.5）：
    - 未到期（`due > today`）：窗口 `[created_at, today]`，**只写 true**（到期前不写 false）；
    - 已到期（`due <= today`）：窗口 `[created_at, due]`，写布尔（未成立态 false）；
    - 无法判定（参考位降级 / 无 level 量类 / 无数据 / 无行情源）→ 不写该键（绝不写 null）；
    - 幂等：已有布尔 `condition_met` 的 c{i} 直接跳过 → 重复执行结果一致；
    - 限速：每处理 `batch_size` 条记录 `await asyncio.sleep(sleep_seconds)`（避免打满 DB/上游）；
    - **不自动执行**：`dry_run=True`（默认）只统计与抽样、零写入；生产须先跑 dry-run 报告 →
      人工确认 → 再以 `dry_run=False` 执行（`scripts/backfill_condition_met.py --execute`）。
    - `limit`：Node 侧 pending 游标分页页大小 / verified 上限；`max_records`：本地截断（演练用）。
    """
    today = shanghai_today().isoformat()
    records = await node_api.list_all_predictions(limit=limit)
    if max_records is not None:
        records = records[: max(0, max_records)]
    # 取数/事件记忆化（同一次运行内跨记录复用；key 含窗口，不串用）
    scan_cache: dict[tuple[str, str, str, str], list[dict[str, object]] | None] = {}
    event_cache: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    scanned = candidates = judgeable = lit = unmet = 0
    unjudgeable = skipped_in_flight = written = write_failed = 0
    samples: list[str] = []
    for pos, record in enumerate(records):
        scanned += 1
        if str(record.get("schema_version") or "") != _BACKFILL_CONDITION_SCHEMA_VERSION:
            continue  # 2.0 旧记录无 conditions 契约
        prediction = record.get("prediction")
        conditions = prediction.get("conditions") if isinstance(prediction, dict) else None
        if not isinstance(conditions, list) or not conditions:
            continue
        record_id = _coerce_record_id(record.get("id"))
        if record_id is None:
            continue
        verification = record.get("verification")
        ver_map = verification if isinstance(verification, dict) else {}
        due_dates = record.get("due_dates")
        due_map = due_dates if isinstance(due_dates, dict) else {}
        horizons = prediction.get("horizons") if isinstance(prediction, dict) else None
        tgt = ""
        if isinstance(horizons, list) and horizons and isinstance(horizons[0], dict):
            tgt = str(horizons[0].get("target") or "")
        code, target_type, _ = await _resolve_verify_target(tgt)
        record_pending = False
        for i, cond in enumerate(conditions):
            if not isinstance(cond, dict):
                continue
            key = f"c{i}"
            existing = ver_map.get(key)
            if isinstance(existing, dict):
                if isinstance(existing.get("condition_met"), bool):
                    continue  # 幂等：已有布尔判定（true/false）→ 不重复写
                if existing.get("type") == "early_exit":
                    continue  # 早退标记 entry 不属条件档位（防御）
            record_pending = True
            anchor_raw = cond.get("anchor")
            anchor: dict[str, object] = (
                cast(dict[str, object], anchor_raw) if isinstance(anchor_raw, dict) else {}
            )
            horizon = anchor.get("horizon")
            due = str(due_map.get(str(horizon)) or "") if horizon else ""
            at_due = bool(due) and due <= today
            # 窗口：已到期 → [created_at, due]；未到期 → [created_at, today]
            window_range = _condition_scan_range(record, due if at_due else today)
            met = await _judge_condition_met_once(
                cond,
                target_type=target_type,
                code=code,
                window_range=window_range,
                scan_cache=scan_cache,
                event_cache=event_cache,
                at_due=at_due,
            )
            if met is None:
                unjudgeable += 1
                continue
            if met is False and not at_due:
                # 未到期只写 true（spec §12.5）：此处的 false 不具备"到期"语义 → 不写
                skipped_in_flight += 1
                continue
            judgeable += 1
            if met:
                lit += 1
            else:
                unmet += 1
            if len(samples) < sample_size:
                samples.append(
                    f"id={record_id} key={key} due={due or '-'} met={str(met).lower()} "
                    f"condition={str(cond.get('condition') or '')[:30]}"
                )
            if dry_run:
                continue
            try:
                await node_api.update_prediction_verification(
                    record_id,
                    key,
                    _condition_met_payload(existing, i, horizon, met, today),
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不炸整批（限速批处理语义）
                write_failed += 1
                logger.warning(
                    "backfill_condition_met_write_failed",
                    id=record_id,
                    key=key,
                    error=str(exc),
                )
        if record_pending:
            candidates += 1
        # 限速：每 batch_size 条记录一次间隔（避免打满 DB/上游）
        if sleep_seconds > 0 and (pos + 1) % max(1, batch_size) == 0:
            await asyncio.sleep(sleep_seconds)
    logger.info(
        "backfill_condition_met_done",
        dry_run=dry_run,
        scanned=scanned,
        candidates=candidates,
        judgeable=judgeable,
        lit=lit,
        unmet=unmet,
        unjudgeable=unjudgeable,
        skipped_in_flight=skipped_in_flight,
        written=written,
        write_failed=write_failed,
    )
    return BackfillConditionMetStats(
        scanned=scanned,
        candidates=candidates,
        judgeable=judgeable,
        lit=lit,
        unmet=unmet,
        unjudgeable=unjudgeable,
        skipped_in_flight=skipped_in_flight,
        written=written,
        write_failed=write_failed,
        samples=tuple(samples),
    )


def _coerce_record_id(value: object) -> int | None:
    """记录 id 归一（Node internal 已归 number；兼容历史 string）。脏值 → None（跳过）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


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
        # D2：Node internal 列表已归一 id 为 number（治本）；兼容历史 string 双保险
        cursor = (
            int(last_id)
            if isinstance(last_id, str) and last_id.isdigit()
            else cast(int | None, last_id)
        )
        if cursor is None or len(batch) < 200:
            break
    if not records:
        logger.info("prediction_validate_no_pending")
        return 0
    updated = 0
    target_counter: dict[str, int] = {}
    # stage① 取数记忆化（终审附带成本项）：同一批次内相同 (target_type, code, 窗口) 只取一次
    scan_cache: dict[tuple[str, str, str, str], list[dict[str, object]] | None] = {}
    # 事件类条件用的事件列表记忆化（Task 5.1）：同批次内相同 (dateFrom, dateTo) 只取一次
    event_cache: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for record in records:
        record_id = record.get("id")
        # D2：Node internal 归一后为 number；兼容历史 string（曾致 isinstance(int) 门禁全量跳过）
        if isinstance(record_id, str) and record_id.isdigit():
            record_id = int(record_id)
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
        # Spec A §4.2/§11：条件化预判两点判定——第①段（到期前扫描）先执行：条件一成立即
        # 点亮 condition_met=true（无 result，Node 端放行中间态），前端洞见卡"待验证"分支
        # 立即亮起；第②段（到期判定）照常写 result。幂等：已点亮/已有 result/已到期者跳过。
        # 单记录异常只 warning 不中断整批（与相邻写回循环同风格）。
        try:
            lit_entries = await _scan_condition_met(
                record, scan_cache=scan_cache, event_cache=event_cache
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prediction_condition_scan_failed",
                id=record_id,
                error=str(exc),
                exc_info=True,
            )
            lit_entries = {}
        for lit_key, lit_entry in lit_entries.items():
            if _should_skip_horizon(verification.get(lit_key)):
                continue  # 兜底：已有 result 的 key 不再写
            try:
                await node_api.update_prediction_verification(record_id, lit_key, lit_entry)
                updated += 1
                logger.info(
                    "prediction_condition_lit",
                    id=record_id,
                    key=lit_key,
                    condition_met=lit_entry.get("condition_met"),
                )
            except Exception as exc:
                logger.warning(
                    "prediction_condition_lit_write_failed",
                    id=record_id,
                    key=lit_key,
                    error=str(exc),
                    exc_info=True,
                )
        # Spec A §4.2/§11：条件化预判双验证调度——3.0 记录对每条 condition 另产 c{i}
        # entry（c{i} key 与 horizon key 并存，A1 early_exit 不冲突）；已存在 result
        # 的 c{i} 幂等跳过。取数/事件记忆化与第①段共用（key 含窗口，不串用）。
        cond_entries = await _verify_conditions(
            record, scan_cache=scan_cache, event_cache=event_cache
        )
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
    """验证统计出口：拉取全部含验证档位的记录（D3 档位级扫描）→ hit_rate_summary/
    baseline_compare → 结构化日志（输出结构化日志供 P2 开 chat 对照与 B3 反哺做决策依据）。

    D3：数据源从 status=verified 改为 pending+verified 全记录（node_api.list_all_predictions），
    否则画像/统计在 long 档（2027）到期前恒空。
    """
    records = await node_api.list_all_predictions()
    if not records:
        return
    entries: list[dict[str, object]] = []
    for rec in records:
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

    读取全部含验证档位的记录（D3 档位级扫描，node_api.list_all_predictions），把每条带
    result 的 verification entry 归到 record 级 target 字符串下，经 ``_resolve_verify_target``
    收敛为稳定 internal_id（stock/index=裸码，sector=ts_code；不直接用 name，防板块改名断
    画像），再 build_validation_profile + 落 ``prediction:profile:{internal_id}`` 缓存——供
    预判 skill 读取 + 迭代闭环消费，避免每次预判拉全量 verified 重算（§8 拉取开销）。
    early_exit-only（无 result）不计入画像（§9-3）。返回写入的靶位数。
    """
    records = await node_api.list_all_predictions()
    if not records:
        return 0
    groups: dict[str, list[dict[str, object]]] = {}
    for rec in records:
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
