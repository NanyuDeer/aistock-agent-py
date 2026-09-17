"""溯源弱反馈观测层（spec §13.3 溯源自身的反馈回路 / 计划 Phase 7 Task 7.1，P6'）。

## 定位：本期只做「观测 + 建议 + 审计」层

把「链上溯源信号」与「预判验证结果」关联聚合 → 产出建议（建议降权 / 建议提级 / 观望）
→ 落审计表（app-api `attribution_feedback_signals`，可查、幂等）。

**默认 `mode="observe"`：只记录建议，不产生任何副作用**——不修改溯源 prompt、驱动类型判定、
预判输入与任何既有写入，也**不真正应用权重**（应用层待积累真实样本后单独立项）。
遵守总纲 §3.4「迭代双链路分离」：弱反馈只用于**溯源侧**信号，**不得直接改写预判**。

## 关联口径

- **样本单元**：某交易日链上 `children[]` 的一个板块（其 `relation` / `events` 即溯源信号）；
- **单元 key（unit）**：见 `SUPPORTED_UNITS`。默认 `relation`——
  `driver_type` 更贴合"溯源模板"语义，但**它并未落库到链或预判记录**
  （`_extract_driver_for_trace` 只在预判时用于注入 prompt/档位白名单，见
  `prediction_service.py:614`），若作为默认键必须回读复盘报告 `market_trace.trace`
  （已实现为可选 unit，但复盘报告不可读时整日样本为 0，脆弱）→ 故默认取**链上持久化的**
  `relation`（`judge_sector_driver_relation` 的确定性产物，唯一稳定的溯源侧标识）；
- **预判关联**：`source_id` 精确匹配 `sector:{链上板块名}:{date}`（§13.5 口径）；
  匹配不上 → 跳过并计数（`unmatched`，不报错、不猜名）；
- **档位样本**：记录 `verification[horizon(short/mid/long)].result ∈ {hit, miss}`；
  `insufficient` 只计数到 detail；`c{i}` 条件层 `result` 与 `condition_met` 布尔
  只进 detail（**不与档位样本混桶**，避免同一记录重复计数污染命中率）；
- **建议规则**：`sample_size < min_samples` → `insufficient`（观望）；否则
  `hit_rate < low_threshold` → `downgrade`；`> high_threshold` → `upgrade`；其余 `hold`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_type

import structlog

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import is_trading_day, prev_trading_day, shanghai_today

logger = structlog.get_logger()

# ── 单元 key 口径（unit） ──
# relation：链上驱动关系（self_driven/market_follow/unknown）——唯一在链上持久化的溯源侧标识（默认）
# relation_sector：同上 + 板块（更细，样本更少）
# sector：仅按板块聚合（不区分 relation；最小口径回退）
# driver_type / driver_type_sector：复用大盘溯源既有映射（_TRACE_CATEGORY_TO_DRIVER），
#   需回读该日复盘报告（可选，成本 +1 请求/日；报告不可读则该日样本跳过并计数）
UNIT_RELATION = "relation"
UNIT_RELATION_SECTOR = "relation_sector"
UNIT_SECTOR = "sector"
UNIT_DRIVER_TYPE = "driver_type"
UNIT_DRIVER_TYPE_SECTOR = "driver_type_sector"

SUPPORTED_UNITS: tuple[str, ...] = (
    UNIT_RELATION,
    UNIT_RELATION_SECTOR,
    UNIT_SECTOR,
    UNIT_DRIVER_TYPE,
    UNIT_DRIVER_TYPE_SECTOR,
)

# ── 建议取值（hold 与 insufficient 均为"观望"，区分样本是否充足，便于审计回溯） ──
SUGGESTION_DOWNGRADE = "downgrade"
SUGGESTION_UPGRADE = "upgrade"
SUGGESTION_HOLD = "hold"
SUGGESTION_INSUFFICIENT = "insufficient"

# ── 运行模式：本期只实现 observe；off 为运维开关（不读不写）；apply 未实现（留待应用层立项） ──
MODE_OFF = "off"
MODE_OBSERVE = "observe"
SUPPORTED_MODES: tuple[str, ...] = (MODE_OFF, MODE_OBSERVE)

_UNKNOWN = "unknown"
_RELATIONS = frozenset({"self_driven", "market_follow", _UNKNOWN})
_HORIZON_KEYS = frozenset({"short", "mid", "long"})
# 样本来源只取板块预判（source_id 前缀）；大盘 review:* / 个股路径不参与（口径见 §13.5）
_SECTOR_SOURCE_PREFIX = "sector:"

# detail/jsonb 体积上限：只留定位用的抽样，不塞全量（审计可读 + 行不膨胀）
_MAX_DETAIL_ITEMS = 10


@dataclass(frozen=True)
class FeedbackSignal:
    """一条弱反馈建议（= 审计表一行，PK=(date, unit_key)）。"""

    date: str
    unit_key: str
    mode: str
    sample_size: int
    hit_count: int
    miss_count: int
    hit_rate: float | None
    suggestion: str
    detail: dict[str, object]

    def payload(self) -> dict[str, object]:
        """上报体（对齐 app-api POST /api/internal/attribution-feedback 字段契约）。"""
        return {
            "date": self.date,
            "unit_key": self.unit_key,
            "mode": self.mode,
            "sample_size": self.sample_size,
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
            "hit_rate": self.hit_rate,
            "suggestion": self.suggestion,
            "detail": self.detail,
        }


@dataclass
class FeedbackRunStats:
    """一次运行的统计出口（CLI 报告 + cron 日志 + 测试断言）。"""

    date: str
    window: int
    unit: str
    mode: str
    dry_run: bool
    dates_scanned: int = 0
    no_chain: int = 0
    driver_unavailable: int = 0
    sectors_scanned: int = 0
    matched: int = 0
    unmatched: int = 0
    records: int = 0
    entries: int = 0
    signals: list[FeedbackSignal] = field(default_factory=list)
    pending_write: int = 0
    written: int = 0
    write_failed: int = 0


@dataclass
class _Bucket:
    """单元 key 维度的累加器（含未匹配计数与抽样）。"""

    sample_size: int = 0
    hit_count: int = 0
    miss_count: int = 0
    insufficient_entries: int = 0
    condition_result_hit: int = 0
    condition_result_miss: int = 0
    condition_met_true: int = 0
    condition_met_false: int = 0
    sectors: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)


# ────────────────────────────── 纯函数：口径与规则 ──────────────────────────────


def unit_needs_driver_type(unit: str) -> bool:
    """该 unit 是否需要回读复盘报告取 driver_type（成本/依赖开关）。"""
    return unit in (UNIT_DRIVER_TYPE, UNIT_DRIVER_TYPE_SECTOR)


def unit_key_for(
    unit: str,
    *,
    relation: str,
    sector: str,
    driver_type: str | None,
) -> str:
    """单元 key（聚合维度）。未知 unit 或 driver 缺失 → ValueError（不静默降级）。"""
    rel = relation if relation in _RELATIONS else _UNKNOWN
    sec = sector or _UNKNOWN
    if unit == UNIT_RELATION:
        return f"relation:{rel}"
    if unit == UNIT_RELATION_SECTOR:
        return f"relation:{rel}:sector:{sec}"
    if unit == UNIT_SECTOR:
        return f"sector:{sec}"
    if unit in (UNIT_DRIVER_TYPE, UNIT_DRIVER_TYPE_SECTOR):
        if not driver_type:
            raise ValueError(f"unit={unit} 需要 driver_type，但该日复盘报告不可用")
        if unit == UNIT_DRIVER_TYPE:
            return f"driver_type:{driver_type}"
        return f"driver_type:{driver_type}:sector:{sec}"
    raise ValueError(f"unsupported attribution feedback unit: {unit}（可选 {SUPPORTED_UNITS}）")


def window_dates(end_date: str, window: int) -> list[str]:
    """窗口口径 = 过去 N 个交易日（含结束时点，降序）。

    结束时点非交易日（周末/节假日）→ 回退到之前最近的交易日：非交易日没有链与预判，
    直接跳过只会让 `no_chain` 虚高。
    """
    if window <= 0:
        return []
    try:
        cursor = date_type.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError(f"invalid date: {end_date}（需要 YYYY-MM-DD）") from exc
    if not is_trading_day(cursor):
        cursor = prev_trading_day(cursor)
    out: list[str] = []
    while len(out) < window:
        out.append(cursor.isoformat())
        cursor = prev_trading_day(cursor)
    return out


def _thresholds() -> tuple[int, float, float]:
    """从配置读阈值并做自洽性守卫（配置错误显式报错，不静默产出错误建议）。"""
    min_samples = int(settings.attribution_feedback_min_samples)
    low = float(settings.attribution_feedback_low_threshold)
    high = float(settings.attribution_feedback_high_threshold)
    if min_samples < 1:
        raise ValueError(f"attribution_feedback_min_samples 必须 >=1（当前 {min_samples}）")
    if not 0.0 <= low < high <= 1.0:
        raise ValueError(
            f"attribution_feedback 阈值配置非法：low={low} / high={high}（要求 0<=low<high<=1）"
        )
    return min_samples, low, high


def suggest(
    hit_rate: float | None,
    sample_size: int,
    *,
    min_samples: int,
    low_threshold: float,
    high_threshold: float,
) -> tuple[str, str]:
    """建议（建议降权 / 建议提级 / 观望）+ 原因码。

    样本不足优先于命中率判定（样本不足时命中率无统计意义）；
    阈值边界取值：**严格小于**才降权、**严格大于**才提级（等于阈值 → 观望）。
    """
    if sample_size < min_samples:
        return SUGGESTION_INSUFFICIENT, "insufficient_sample"
    if hit_rate is None:
        return SUGGESTION_INSUFFICIENT, "no_result_entries"
    if hit_rate < low_threshold:
        return SUGGESTION_DOWNGRADE, "low_hit_rate"
    if hit_rate > high_threshold:
        return SUGGESTION_UPGRADE, "high_hit_rate"
    return SUGGESTION_HOLD, "mid_hit_rate"


def build_signal(
    *,
    date: str,
    unit_key: str,
    mode: str,
    sample_size: int,
    hit_count: int,
    miss_count: int,
    detail: dict[str, object],
) -> FeedbackSignal:
    """装配信号（命中率 4 位小数；样本 0 → hit_rate=None，不写 0 冒充 0%）。"""
    min_samples, low, high = _thresholds()
    hit_rate = round(hit_count / sample_size, 4) if sample_size else None
    suggestion, reason = suggest(
        hit_rate, sample_size, min_samples=min_samples, low_threshold=low, high_threshold=high
    )
    return FeedbackSignal(
        date=date,
        unit_key=unit_key,
        mode=mode,
        sample_size=sample_size,
        hit_count=hit_count,
        miss_count=miss_count,
        hit_rate=hit_rate,
        suggestion=suggestion,
        detail={**detail, "reason": reason},
    )


# ────────────────────────────── 读侧：记录索引与验证计数 ──────────────────────────────


def _index_sector_predictions(
    records: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """source_id → 记录列表（只收板块预判 `sector:*`；其余来源不参与弱反馈口径）。"""
    index: dict[str, list[dict[str, object]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or not source_id.startswith(_SECTOR_SOURCE_PREFIX):
            continue
        index.setdefault(source_id, []).append(record)
    return index


def _count_verification(records: list[dict[str, object]]) -> _Bucket:
    """汇总该 source_id 下全部记录的验证结果（档位级样本 + 条件层 detail 分开计数）。"""
    bucket = _Bucket()
    for record in records:
        verification = record.get("verification")
        if not isinstance(verification, dict):
            continue
        for key, entry in verification.items():
            if not isinstance(entry, dict):
                continue
            result = entry.get("result")
            if key in _HORIZON_KEYS:
                if result == "hit":
                    bucket.hit_count += 1
                    bucket.sample_size += 1
                elif result == "miss":
                    bucket.miss_count += 1
                    bucket.sample_size += 1
                elif result == "insufficient":
                    bucket.insufficient_entries += 1
                continue
            if not (key.startswith("c") and key[1:].isdigit()):
                continue
            if result == "hit":
                bucket.condition_result_hit += 1
            elif result == "miss":
                bucket.condition_result_miss += 1
            met = entry.get("condition_met")
            if met is True:
                bucket.condition_met_true += 1
            elif met is False:
                bucket.condition_met_false += 1
    return bucket


class _TraceView:
    """复盘报告 `market_trace.trace` 原始 dict 的最小只读视图。

    `_extract_driver_for_trace` 只读 `candidates` / `primary_chain_id`（内部已兼容 dict
    与对象两种候选表示），故无需走 `MarketTraceResult` 契约校验——本层只要 driver 口径，
    溯源结果的契约校验在写入侧已完成；shape 不符 → driver 落保守档（不抛错、不硬造）。
    """

    def __init__(self, data: dict[str, object]) -> None:
        self.candidates = data.get("candidates")
        self.primary_chain_id = data.get("primary_chain_id")


async def _driver_type_for_date(date_str: str) -> str | None:
    """回读该日复盘报告的大盘溯源主因类别 → driver_type（复用既有映射，不新增口径）。

    失败/无报告/无 trace → None（调用方整日跳过并计数，**不猜 driver**）。
    """
    from aistock_agent.services.prediction_service import _extract_driver_for_trace  # noqa: PLC0415

    try:
        reports = await node_api.list_analysis_reports("review", date_str)
    except Exception:  # noqa: BLE001 —— 观测层读失败降级，不影响主链路
        logger.warning("attribution_feedback_report_read_failed", date=date_str, exc_info=True)
        return None
    for report in reports or []:
        if not isinstance(report, dict) or report.get("status") != "completed":
            continue
        content = report.get("content")
        market_trace = content.get("market_trace") if isinstance(content, dict) else None
        trace = market_trace.get("trace") if isinstance(market_trace, dict) else None
        if not isinstance(trace, dict):
            continue
        return _extract_driver_for_trace(_TraceView(trace))
    return None


# ────────────────────────────── 聚合 ──────────────────────────────


def _detail(
    *,
    unit: str,
    window: int,
    stats: FeedbackRunStats,
    bucket: _Bucket,
    unmatched: list[str],
) -> dict[str, object]:
    """审计明细（bounded；含未匹配抽样，供定位 source_id 口径漂移）。"""
    min_samples, low, high = _thresholds()
    return {
        "unit": unit,
        "window": window,
        "dates_scanned": stats.dates_scanned,
        "no_chain": stats.no_chain,
        "min_samples": min_samples,
        "low_threshold": low,
        "high_threshold": high,
        "insufficient_entries": bucket.insufficient_entries,
        "condition_result_hit": bucket.condition_result_hit,
        "condition_result_miss": bucket.condition_result_miss,
        "condition_met_true": bucket.condition_met_true,
        "condition_met_false": bucket.condition_met_false,
        "sectors": sorted(set(bucket.sectors))[:_MAX_DETAIL_ITEMS],
        "source_ids_sample": bucket.source_ids[:_MAX_DETAIL_ITEMS],
        "unmatched_count": stats.unmatched,
        "unmatched_sectors": sorted(set(unmatched))[:_MAX_DETAIL_ITEMS],
    }


async def aggregate_attribution_feedback(
    *,
    end_date: str,
    window: int,
    unit: str,
    mode: str,
) -> FeedbackRunStats:
    """窗口内「链上溯源信号 × 预判验证结果」聚合（只读，不写任何东西）。"""
    _thresholds()  # 配置自洽性守卫（先于任何读，配置错即报）
    stats = FeedbackRunStats(
        date=end_date, window=window, unit=unit, mode=mode, dry_run=True
    )
    dates = window_dates(end_date, window)
    records = await node_api.list_all_predictions()
    stats.records = len(records)
    index = _index_sector_predictions(records)

    buckets: dict[str, _Bucket] = {}
    unmatched: list[str] = []
    for date_str in dates:
        stats.dates_scanned += 1
        chain = await node_api.get_attribution_chain(date_str)
        if not isinstance(chain, dict) or not chain:
            stats.no_chain += 1
            continue
        driver_type: str | None = None
        if unit_needs_driver_type(unit):
            driver_type = await _driver_type_for_date(date_str)
            if driver_type is None:
                stats.driver_unavailable += 1
                continue
        children = chain.get("children")
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            sector = str(child.get("sector") or "").strip()
            if not sector:
                continue
            stats.sectors_scanned += 1
            source_id = f"{_SECTOR_SOURCE_PREFIX}{sector}:{date_str}"
            matched_records = index.get(source_id)
            if not matched_records:
                # 口径不匹配（链上板块名 ≠ 预判 source_id 的 resolved 名）→ 跳过并计数
                stats.unmatched += 1
                unmatched.append(sector)
                continue
            stats.matched += 1
            counted = _count_verification(matched_records)
            stats.entries += counted.sample_size
            relation = str(child.get("relation") or "")
            key = unit_key_for(
                unit, relation=relation, sector=sector, driver_type=driver_type
            )
            bucket = buckets.setdefault(key, _Bucket())
            bucket.sample_size += counted.sample_size
            bucket.hit_count += counted.hit_count
            bucket.miss_count += counted.miss_count
            bucket.insufficient_entries += counted.insufficient_entries
            bucket.condition_result_hit += counted.condition_result_hit
            bucket.condition_result_miss += counted.condition_result_miss
            bucket.condition_met_true += counted.condition_met_true
            bucket.condition_met_false += counted.condition_met_false
            bucket.sectors.append(sector)
            bucket.source_ids.append(source_id)

    for key in sorted(buckets):
        bucket = buckets[key]
        stats.signals.append(
            build_signal(
                date=end_date,
                unit_key=key,
                mode=mode,
                sample_size=bucket.sample_size,
                hit_count=bucket.hit_count,
                miss_count=bucket.miss_count,
                detail=_detail(
                    unit=unit, window=window, stats=stats, bucket=bucket, unmatched=unmatched
                ),
            )
        )
    if stats.unmatched:
        # 未匹配率高通常意味着链上板块名与预判 resolved 名口径漂移（R14 同源），需人工核查
        logger.info(
            "attribution_feedback_unmatched",
            date=end_date,
            unmatched=stats.unmatched,
            matched=stats.matched,
            sample=sorted(set(unmatched))[:_MAX_DETAIL_ITEMS],
        )
    return stats


# ────────────────────────────── 写侧：审计上报 ──────────────────────────────


class AttributionFeedbackStore:
    """审计上报（POST /api/internal/attribution-feedback）。

    路径必须带 /api 前缀：app-api 把本 router 挂在 /api 下（R13 教训——不带 /api 会命中
    错误 router 恒 404，且 `data_client.post` 吞错返回 None → 静默不落库）。
    **失败只 warning 不抛出**：观测层不得影响任何既有链路（app-api 未部署时仅告警）。
    """

    PATH = "/api/internal/attribution-feedback"

    def __init__(self) -> None:
        self.node_api = node_api

    async def save(self, signal: FeedbackSignal) -> bool:
        try:
            result = await self.node_api.post(self.PATH, signal.payload())
        except Exception:  # noqa: BLE001 —— 上报失败不得冒泡
            logger.warning(
                "attribution_feedback_save_failed",
                date=signal.date,
                unit_key=signal.unit_key,
                exc_info=True,
            )
            return False
        if result is None:
            logger.warning(
                "attribution_feedback_save_failed",
                date=signal.date,
                unit_key=signal.unit_key,
                error="node_api.post 返回 None（请求失败或业务码异常）",
            )
            return False
        logger.info(
            "attribution_feedback_saved",
            date=signal.date,
            unit_key=signal.unit_key,
            suggestion=signal.suggestion,
            sample_size=signal.sample_size,
        )
        return True


# ────────────────────────────── 入口 ──────────────────────────────


async def run_attribution_feedback(
    *,
    date: str | None = None,
    window: int | None = None,
    unit: str | None = None,
    mode: str | None = None,
    dry_run: bool = True,
) -> FeedbackRunStats:
    """观测层入口：聚合 → 生成建议 →（非 dry-run 时）上报审计。

    `dry_run=True`（CLI 默认）只统计不写；`mode="off"` 直接返回（运维开关，不读不写）。
    """
    end_date = date or shanghai_today().isoformat()
    window_value = window if window is not None else int(settings.attribution_feedback_window)
    unit_value = unit or settings.attribution_feedback_unit
    mode_value = mode or settings.attribution_feedback_mode
    if mode_value not in SUPPORTED_MODES:
        # 配置值非法（如未来 apply 尚未实现）→ 保守回落 observe 并告警，不静默照写
        logger.warning("attribution_feedback_mode_unknown", mode=mode_value, fallback=MODE_OBSERVE)
        mode_value = MODE_OBSERVE
    if mode_value == MODE_OFF:
        logger.info("attribution_feedback_skipped_by_mode", date=end_date)
        return FeedbackRunStats(
            date=end_date, window=window_value, unit=unit_value, mode=MODE_OFF, dry_run=dry_run
        )
    if unit_value not in SUPPORTED_UNITS:
        raise ValueError(f"unsupported attribution feedback unit: {unit_value}")

    stats = await aggregate_attribution_feedback(
        end_date=end_date, window=window_value, unit=unit_value, mode=mode_value
    )
    stats.dry_run = dry_run
    stats.pending_write = len(stats.signals)
    if not dry_run:
        store = AttributionFeedbackStore()
        for signal in stats.signals:
            if await store.save(signal):
                stats.written += 1
            else:
                stats.write_failed += 1
    logger.info(
        "attribution_feedback_done",
        date=end_date,
        window=window_value,
        unit=unit_value,
        mode=mode_value,
        dry_run=dry_run,
        signals=len(stats.signals),
        matched=stats.matched,
        unmatched=stats.unmatched,
        entries=stats.entries,
        written=stats.written,
        write_failed=stats.write_failed,
    )
    return stats


def signal_as_json(signal: FeedbackSignal) -> str:
    """信号 JSON 串（CLI 输出/排查用）。"""
    return json.dumps(signal.payload(), ensure_ascii=False)
