"""case_sourcers.py — 产片源（case sourcing）provider 注册表（二期）。

provider 是"候选切片输入"的生产者：给定 SourceContext，返回 CaseCandidate 列表。
流水线（case_pipeline.build_cases_for_adapter）按 adapter.case_sources 采集候选。
注册表清单封闭：adapter 引用的 provider 名必须登记在本模块 SOURCE_PROVIDERS。
"""

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import structlog

from aistock_agent.iterate.adapters import IterableAgentAdapter
from aistock_agent.iterate.case_scanner import find_recent_trading_day
from aistock_agent.schemas.target import Target
from aistock_agent.services.data_client import NodeApiClient
from aistock_agent.services.event_store import is_major_event, load_event_scrape
from aistock_agent.services.market_trace_snapshot import build_market_trace_snapshot
from aistock_agent.services.target_profile import (
    get_iterate_threshold,
    get_profile,
    make_target,
)
from aistock_agent.skills.prediction_validation import read_validation_profile
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

#: 中台 direction 值域 → GT direction_hint 值域映射（四期最终评审 C2）。
#: 中台（normalize_event/event_scoring_llm）direction ∈ {positive, negative, neutral}；
#: GT 方向先验白名单 ∈ {bullish, bearish, neutral}（ground_truth.py:168）——
#: 不映射则 positive/negative 原样写入 meta.direction_hint 不会被注入。
_DIRECTION_MAP = {"positive": "bullish", "negative": "bearish", "neutral": "neutral"}


#: 通用产片候选：与 build_case 入参一一对应，覆盖 adapter.data_deps 所需切片字段
@dataclass
class CaseCandidate:
    event_title: str
    event_time: datetime
    telegraph_records: list[dict[str, object]]
    market_snapshot: dict[str, object] | None = None
    industry_graph: dict[str, object] | None = None
    meta: dict[str, object] | None = None


@dataclass(frozen=True)
class SourceContext:
    agent_id: str
    params: dict[str, object]
    data_dir: Path | None
    force: bool = False  # CLI --force 透传（快照数据不足时允许强制产片）


async def market_close_snapshot(ctx: SourceContext) -> list[CaseCandidate]:
    """review 产片源：收盘快照（最近交易日，或 params["date"] 指定的历史交易日回补）。"""
    target_day = ctx.params.get("date")
    if isinstance(target_day, str) and target_day:
        # 历史回补（三期）：直接用指定交易日（build_market_trace_snapshot 内部校验
        # 交易日/complete，失败抛 MarketTraceSnapshotUnavailable → provider 抛错 →
        # source_cases 降级）
        day = target_day
    else:
        recent_day = await find_recent_trading_day()
        if recent_day is None:
            raise RuntimeError("无法发现最近交易日（Node close-snapshot/last-close 均失败）")
        day = recent_day
    snapshot = await build_market_trace_snapshot(day)
    # 三期评审（IMP-3）：date 分支必须校验回补日期一致性。build_market_trace_snapshot
    # 内部有 last-close 兜底链（Node 409 非交易日/数据缺失 → 返回"最近交易日"快照），
    # 若不加校验，指定日期回补失败会静默产出"最近交易日"case（trade_date 与请求
    # date 不一致），违背"回补失败 → provider 抛错 → source_cases 降级 0 候选"的硬约束。
    # 校验放在 snapshot 获取后、后续字段取值前；不影响无 date 分支（target_day 非 str/空）。
    if isinstance(target_day, str) and target_day:
        actual = str(getattr(snapshot, "trade_date", ""))
        if actual != target_day:
            raise RuntimeError(
                "历史回补日期不一致："
                f"期望 {target_day}，Node 快照实际 {actual or '空'}（非交易日或数据缺失，拒绝产片）"
            )

    trade_date = str(getattr(snapshot, "trade_date", ""))
    captured_at = getattr(snapshot, "captured_at", None)
    if captured_at is None:
        raise RuntimeError("收盘快照缺少 captured_at，拒绝产片")
    discovery = getattr(snapshot, "phenomenon_discovery", None)
    primary = getattr(discovery, "primary", None)
    event_title = str(getattr(primary, "summary", "")) or f"A股收盘{trade_date}"

    # snapshot 跨类型边界（生产 MarketTraceSnapshot / 测试注入 object），cast Any
    # 调用 model_dump 避免 mypy attr-defined（与原 build_iterate_cases 一致）。
    snapshot_dict = cast("dict[str, object]", cast("Any", snapshot).model_dump(mode="json"))
    insufficient = _snapshot_data_sufficient(snapshot_dict)
    if insufficient and not ctx.force:
        logger.warning(
            "review_case_rejected_insufficient_snapshot",
            reasons=insufficient,
            snapshot_id=snapshot_dict.get("snapshot_id"),
        )
        raise RuntimeError(f"review case 数据不足拒绝产片：{'; '.join(insufficient)}")

    sources = cast("dict[str, object]", snapshot_dict.get("sources", {}))
    telegraph_records = [
        _source_to_record(cast("dict[str, object]", src))
        for src in sources.values()
        if isinstance(src, dict) and src.get("kind") in {"event_evidence", "market_fact"}
    ]
    # 三期服务器实测修复（2026-08-14）：历史回补 case 的事件锚点必须是目标交易日
    # （15:30 CST = UTC 07:30），而非构建时刻 captured_at——否则 event_time/case_id
    # 前缀错标为构建日（如回补 08-07 却标 08-14），T 窗口锚定与去重语义全错。
    event_time = (
        _close_time_for_day(day)
        if isinstance(target_day, str) and target_day
        else cast(datetime, captured_at)
    )
    return [
        CaseCandidate(
            event_title=event_title,
            event_time=event_time,
            telegraph_records=telegraph_records,
            market_snapshot=snapshot_dict,
            industry_graph=await _collect_industry_graph(event_time=event_time),
            meta={"snapshot_kind": "full", "t_window": "close"},
        )
    ]


async def sector_close_snapshot(ctx: SourceContext) -> list[CaseCandidate]:
    """sector 板块归因产片源（Spec D）：大盘快照 top_losers → 每板块一个历史案例。

    板块异动切片语义与 market_close_snapshot 同款：date 参数走历史回补
    （build_market_trace_snapshot 内部校验交易日/complete），否则最近交易日。
    从快照 a_share.sectors.top_losers 取领跌板块列表（快照缺 sectors/top_losers
    返回 []，不炸产片源）；每板块产一个 CaseCandidate，meta 携带 {sector_row}
    （板块行情条目，含 pct_change/net_amount/lead_stock/company_num，Node 原样），
    供 TargetProfile.snapshot_builder=build_sector_snapshot 重建归因输入。
    market_snapshot 传完整快照 dict（对齐 market_close_snapshot——build_case 的
    _validate_market_snapshot 强制完整 MarketTraceSnapshot 契约，只传 a_share
    切片会在产片链校验失败，候选全被拒）。单板块条目畸形（非 dict/无名）仅跳过。
    """
    target_day = ctx.params.get("date")
    if isinstance(target_day, str) and target_day:
        day = target_day
    else:
        recent_day = await find_recent_trading_day()
        if recent_day is None:
            raise RuntimeError("无法发现最近交易日（Node close-snapshot/last-close 均失败）")
        day = recent_day
    snapshot = await build_market_trace_snapshot(day)
    # 对齐 market_close_snapshot（IMP-3）：date 回补必须校验一致性——build_market_trace_snapshot
    # 有 last-close 兜底链，不校验会静默产出"最近交易日"快照但 event_time 锚定请求日，
    # 切片内容与 case_id 前缀错位（防复现已修事故）。
    if isinstance(target_day, str) and target_day:
        actual = str(getattr(snapshot, "trade_date", ""))
        if actual != target_day:
            raise RuntimeError(
                "历史回补日期不一致："
                f"期望 {target_day}，Node 快照实际 {actual or '空'}（非交易日或数据缺失，拒绝产片）"
            )
    # snapshot 跨类型边界（生产 MarketTraceSnapshot / 测试注入 object），cast Any
    # 调用 model_dump 避免 mypy attr-defined（与 market_close_snapshot 一致）。
    snapshot_dict = cast("dict[str, object]", cast("Any", snapshot).model_dump(mode="json"))
    a_share = snapshot_dict.get("a_share")
    if not isinstance(a_share, dict):
        return []
    sectors = a_share.get("sectors")
    if not isinstance(sectors, dict):
        return []
    losers = sectors.get("top_losers")
    if not isinstance(losers, list):
        return []
    candidates: list[CaseCandidate] = []
    for los in losers:
        if not isinstance(los, dict):
            continue
        name = str(los.get("name") or "")
        if not name:
            continue
        candidates.append(
            CaseCandidate(
                event_title=f"{name} 板块异动",
                event_time=_close_time_for_day(day),
                telegraph_records=[],
                market_snapshot=snapshot_dict,
                meta={"sector_row": los, "t_window": "close"},
            )
        )
    return candidates


async def telegraph_keyword_scan(ctx: SourceContext) -> list[CaseCandidate]:
    """event_analyst 产片源：window_days 天内电报重大事件（迁移自 scan_major_events 调用点）。"""
    from aistock_agent.iterate.case_scanner import scan_major_events

    days = int(cast("int", ctx.params.get("window_days", 30)))
    events = await scan_major_events(days)
    return [
        CaseCandidate(
            event_title=str(event["event_title"]),
            event_time=_dt_from_iso(str(event["event_time"])),
            telegraph_records=cast("list[dict[str, object]]", event["telegraph_records"]),
            meta={"t_window": "event"},
        )
        for event in events
    ]


async def prediction_verified_scan(ctx: SourceContext) -> list[CaseCandidate]:
    """prediction 产片源（Spec C §4.3）：从已验证的 prediction 记录切历史案例。

    只切 schema_version=3.0（现役条件化预判）且 verification 非空的记录——有
    due_dates + hit/miss，是「验证驱动迭代」的标准答案锚点。每记录一条候选，
    event_time 锚定 source_id 内嵌的交易日（对齐 _close_time_for_day 15:30 CST），
    meta 携带 {record_id, target, trade_date, prediction, due_dates, verification}
    供回放/评估消费。回放输入的历史市场快照按 data_deps "market" 在切片落地时
    由 TargetProfile.snapshot_builder 补齐（全局 §2.3/§4.1 衔接 Spec D）。
    无满足条件的记录返回 []（不炸产片源）；单条 source_id 不可解析日期仅跳过。
    """
    records = await NodeApiClient().list_verified_predictions(limit=500)
    candidates: list[CaseCandidate] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("schema_version", "")) != "3.0":
            continue
        verification = rec.get("verification")
        if not isinstance(verification, dict) or not verification:
            continue
        prediction = rec.get("prediction")
        if not isinstance(prediction, dict):
            continue
        target = _first_target_str(prediction)
        trade_date = _source_trade_date(str(rec.get("source_id", "")))
        if target is None or trade_date is None:
            logger.warning(
                "prediction_verified_scan_skip_missing_anchor",
                record_id=rec.get("id"),
                source_id=rec.get("source_id"),
            )
            continue
        candidates.append(
            CaseCandidate(
                event_title=f"预判验证 {target}（{trade_date}）",
                event_time=_close_time_for_day(trade_date),
                telegraph_records=[],
                meta={
                    "record_id": rec.get("id"),
                    "target": target,
                    "trade_date": trade_date,
                    "prediction": prediction,
                    "due_dates": rec.get("due_dates", {}),
                    "verification": verification,
                    "t_window": "prediction",
                },
            )
        )
    return candidates


def _first_target_str(prediction: dict[str, object]) -> str | None:
    """取 prediction 首个非空 target 字符串（预判产片分组锚点）。"""
    horizons = prediction.get("horizons")
    if isinstance(horizons, list):
        for h in horizons:
            if isinstance(h, dict) and h.get("target"):
                return str(h["target"])
    return None


#: 迭代触发判定的方向场景（Spec C §5.3 分层阈值的 scenario 轴；方向中性归 up 侧保守）。
_SCENARIOS = ("up", "down")


async def _prediction_case_source_eligible(
    target: Target, horizon: str, scenario: str
) -> bool:
    """prediction 案例是否达迭代触发条件（Spec C §5.3 阈值分层 + §5.3 sufficient_sample 闸门）。

    读 target 历史验证画像（缓存优先，read_validation_profile），样本充足
    （sufficient_sample=True）且命中率低于 ``get_iterate_threshold(target, horizon, scenario)``
    分层阈值 → True（应产片）。小样本 / 未命中阈值 → False（不触发不耗 token）。
    """
    profile = await read_validation_profile(target, horizon)
    if not bool(profile.get("sufficient_sample", False)):
        return False
    hit_rate = float(cast(float, profile.get("hit_rate", 0.0)))
    threshold = get_iterate_threshold(target, horizon, scenario)
    return hit_rate < threshold


async def _prediction_candidate_kept(candidate: CaseCandidate) -> bool:
    """prediction 候选是否保留：任一 default horizon×scenario 触发即保留。

    候选 meta.target 无法解析为首类 Target（unknown 抽象词，make_target None）→
    保守丢弃（不产片，防误触发）；解析成功则按 profile.default_horizons × 方向
    场景逐个判 ``_prediction_case_source_eligible``，任一命中 True 即短路保留。
    """
    meta = candidate.meta
    target_raw = meta.get("target") if isinstance(meta, dict) else None
    if not isinstance(target_raw, str) or not target_raw:
        return False
    target = make_target(target_raw)
    if target is None:
        return False
    for horizon in get_profile(target).default_horizons:
        for scenario in _SCENARIOS:
            if await _prediction_case_source_eligible(target, horizon, scenario):
                return True
    return False


def _source_trade_date(source_id: str) -> str | None:
    """从 source_id（如 "review:2026-08-14"）提取交易日 YYYY-MM-DD；无则 None。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", source_id)
    return m.group(1) if m else None


async def event_store_scan(ctx: SourceContext) -> list[CaseCandidate]:
    """事件库产片源（四期）：近 window_days 天事件库重大事件 → CaseCandidate。
    消费统一事件抓取中台（event_scraper）入库数据（只读，不改中台）；
    is_major_event（impact_score >= 4）过滤；telegraph_records 用事件
    summary/content（语料进 GT corpus）；meta 带 direction_hint（事件方向先验，
    GT 生成消费）。

    load_event_scrape/is_major_event/shanghai_today 为模块级 import：四期 brief
    用例 patch 目标是 case_sourcers 模块属性（对齐 market_close_snapshot 的
    find_recent_trading_day 先例——brief 用例 patch 模块属性时提升为模块级）。
    """
    days = int(cast("int", ctx.params.get("window_days", 30)))
    today = shanghai_today()
    candidates: list[CaseCandidate] = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        try:
            events = await load_event_scrape(day)
        except Exception as exc:  # noqa: BLE001 — 单日读取失败降级跳过
            logger.warning("event_store_scan_day_failed", date=day, error=str(exc))
            continue
        for event in events:
            if not is_major_event(event):
                continue
            # 中台契约（四期最终评审 C1 修复）：
            # - score_date 是纯日期 "2026-08-14"（评分日锚点，event_store.normalize_event 注释）；
            # - scrape_at 是上海 naive 时间 "2026-08-14 10:00:00"（无时区）。
            # 旧实现把 score_date（naive 被 _dt_from_iso 补 UTC → 当日 00:00 UTC）当
            # event_time，而 telegraph_records.time 用 scrape_at（naive 被
            # _parse_record_time 补 UTC → 10:00 UTC）→ _record_time_le(10:00, 00:00)
            # False → 事件记录全被 T 窗口过滤 → cls_telegraph 空 → 空壳 case。
            # 修复：scrape_at 补 +08:00 转 aware 作 event_time，record_time 存 aware
            # ISO（_parse_record_time 对 aware 不再二次补 UTC），比较同轴。
            # scrape_at 缺失时兜底用评分日零点（naive 补 UTC）作锚点，保持防空壳优先。
            try:
                scrape_at_raw = str(event.get("scrape_at", "")).strip()
                if scrape_at_raw:
                    event_time = _dt_from_iso(f"{scrape_at_raw}+08:00")
                else:
                    event_time = _dt_from_iso(str(event.get("score_date", "")))
                record_time = event_time.isoformat()
            except (TypeError, ValueError, KeyError):
                # I1：单条事件时间畸形（不可解析）只跳过该条 + warning，不炸整源
                logger.warning(
                    "event_store_scan_skip_malformed_time",
                    event_id=str(event.get("event_id", ""))[:32],
                    score_date=str(event.get("score_date", "")),
                    scrape_at=str(event.get("scrape_at", "")),
                )
                continue
            candidates.append(
                CaseCandidate(
                    event_title=str(event["title"]),
                    event_time=event_time,
                    telegraph_records=[
                        {
                            "time": record_time,
                            "title": str(event["title"]),
                            "content": str(event.get("summary", "")),
                            "url": str(event.get("url", "")),
                        }
                    ],
                    meta={
                        "t_window": "event",
                        "source": "event_store",
                        # C2：中台 positive/negative/neutral → GT 白名单
                        # bullish/bearish/neutral；未映射值写空串（不注入先验）
                        "direction_hint": _DIRECTION_MAP.get(
                            str(event.get("direction", "")), ""
                        ),
                    },
                )
            )
    return candidates


async def source_cases(
    adapter: IterableAgentAdapter,
    *,
    data_dir: Path | None = None,
    force: bool = False,
) -> list[CaseCandidate]:
    """按 adapter.case_sources 逐个 provider 采集候选；单源失败降级跳过并告警。

    四期：合并后按事件标题指纹去重——同指纹候选只保留第一个（case_sources 顺序
    保证事件库在前优先），去重范围限单次调用内。
    """
    candidates: list[CaseCandidate] = []
    for spec in adapter.case_sources:
        provider = SOURCE_PROVIDERS.get(spec.provider)
        if provider is None:
            logger.error(
                "case_source_provider_missing", provider=spec.provider, agent=adapter.agent_id
            )
            continue
        try:
            candidates.extend(
                await provider(
                    SourceContext(
                        agent_id=adapter.agent_id,
                        params=spec.params,
                        data_dir=data_dir,
                        force=force,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 — 单源失败不阻断其他源
            logger.warning(
                "case_source_provider_failed",
                provider=spec.provider,
                agent=adapter.agent_id,
                error=str(exc),
            )
    # 四期：跨源同事件指纹去重——同指纹候选只保留第一个（case_sources 顺序
    # 保证事件库在前优先）；保持各源内部产出顺序。范围限单次调用内（跨日不重叠）。
    seen: set[str] = set()
    deduped: list[CaseCandidate] = []
    for candidate in candidates:
        fp = _candidate_fingerprint(candidate)
        if fp in seen:
            logger.info(
                "case_source_candidate_deduped", fingerprint=fp, title=candidate.event_title
            )
            continue
        seen.add(fp)
        deduped.append(candidate)
    return deduped


def _candidate_fingerprint(candidate: CaseCandidate) -> str:
    """事件标题指纹（去空白/标点归一化 → sha1）：跨源同事件识别（四期）。"""
    normalized = re.sub(r"[\s\W_]+", "", candidate.event_title).lower()
    return hashlib.sha1(normalized.encode()).hexdigest()


#: provider 注册表（清单封闭：新 provider 必须登记于此）
SOURCE_PROVIDERS: dict[str, Callable[[SourceContext], Awaitable[list[CaseCandidate]]]] = {
    "market_close_snapshot": market_close_snapshot,
    "sector_close_snapshot": sector_close_snapshot,
    "event_store_scan": event_store_scan,
    "telegraph_keyword_scan": telegraph_keyword_scan,
    "prediction_verified_scan": prediction_verified_scan,
}


def _source_to_record(source: dict[str, object]) -> dict[str, object]:
    """SourceRecord dict → build_case 的 telegraph_records 形状（迁移自原实现）。"""
    occurred = source.get("occurred_at")
    return {
        "time": str(occurred) if occurred else "",
        "title": str(source.get("title", "")),
        "content": str(source.get("content", "")),
        "url": str(source.get("url", "")),
    }


def _snapshot_data_sufficient(snapshot_dict: dict[str, object]) -> list[str]:
    """检查收盘快照是否有足够的 A 股数据支撑归因分析；返回缺失原因列表（空 = 足够）。

    回归（case_20260731_us_market_surge 服务器全 0 分事故）：build_market_trace_snapshot
    的 normalize_a_share 只做字段复制不校验完整性——Node close-snapshot 返回
    status=complete + coverage.complete=true 但 indexes 等字段缺失时，a_share
    为空壳仍产片成功，空壳 case 进闭环跑满 max_rounds 全部 0 分（浪费 LLM 预算）。
    以 a_share.indexes 为关键闸门：A 股指数是 review agent 归因（方向/板块/驱动）
    的事实基础，缺失则无法产出有效分析，必须拒绝产片（force=True 可跳过）。
    """
    reasons: list[str] = []
    a_share = snapshot_dict.get("a_share")
    if not isinstance(a_share, dict):
        reasons.append("a_share 缺失")
        return reasons
    indexes = a_share.get("indexes")
    if not isinstance(indexes, dict) or not indexes:
        reasons.append("a_share.indexes 为空")
    missing = snapshot_dict.get("missing_fields")
    if isinstance(missing, list) and "a_share.indexes" in missing:
        reasons.append("missing_fields 含 a_share.indexes")
    return reasons


def _dt_from_iso(value: str) -> datetime:
    """ISO 字符串 → aware datetime；naive 兜底 UTC（防止 event_time 无时区进切片）。"""
    dt = datetime.fromisoformat(value)
    # 按要求保留 timezone.utc（brief 指定 import timezone；UP017 建议 datetime.UTC 等价）
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)  # noqa: UP017


def _close_time_for_day(day: str) -> datetime:
    """目标交易日 15:30 CST（= UTC 07:30）：历史回补 case 的事件锚点（三期实测修复）。

    与 Node getCloseSnapshotByDate 的伪时刻构造一致；day 格式 YYYY-MM-DD。
    """
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, 7, 30, tzinfo=timezone.utc)  # noqa: UP017


async def _collect_industry_graph(*, event_time: datetime) -> dict[str, object] | None:
    """采集行业图谱快照（B-5）：Node /internal/industry/graph + 三时间戳标记。

    返回结构：
    {"chains": [...], "snapshot_generated_at": <采集时刻 ISO>,
     "graph_update_time": <Node payload 内的时间，缺失用采集时刻>,
     "event_time": <事件时间 ISO>, "posterior_exposure": False}
    采集失败重试 1 次（四期加固：三期实测 CLI 产片时单次调用超时/连接池异常，
    Node 端点 200；重试后仍失败降级 None 不阻断产片）。
    """
    # 采集时刻用上海时区（与切片事件时间对齐；datetime.now 本地时区在服务器
    # 与容器间可能漂移，B-5 三时间戳要求可比较）
    from aistock_agent.utils.date import shanghai_now

    # 四期：重试 1 次（共 2 次尝试）——首次异常或非 dict 均二次重试；NodeApiClient
    # 为模块级 import（brief 用例 patch 目标是 case_sourcers 模块属性，对齐
    # event_store_scan 的模块级 import 先例；data_client 已被 market_trace_snapshot
    # 传递加载，无循环依赖）。
    payload: object = None
    for attempt in range(2):
        try:
            payload = await NodeApiClient().get_industry_graph_full()
            if isinstance(payload, dict):
                break
        except Exception as exc:  # noqa: BLE001 — 单次失败重试，不阻断产片
            logger.warning("industry_graph_collect_failed", attempt=attempt, error=str(exc))
    if not isinstance(payload, dict):
        return None
    collected_at = shanghai_now().isoformat()
    return {
        "chains": payload.get("chains", []),
        "snapshot_generated_at": collected_at,
        "graph_update_time": str(payload.get("graph_update_time", collected_at)),
        "event_time": event_time.isoformat(),
        "posterior_exposure": False,
    }
