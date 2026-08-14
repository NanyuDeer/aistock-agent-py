"""case_sourcers.py — 产片源（case sourcing）provider 注册表（二期）。

provider 是"候选切片输入"的生产者：给定 SourceContext，返回 CaseCandidate 列表。
流水线（case_pipeline.build_cases_for_adapter）按 adapter.case_sources 采集候选。
注册表清单封闭：adapter 引用的 provider 名必须登记在本模块 SOURCE_PROVIDERS。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import structlog

from aistock_agent.iterate.adapters import IterableAgentAdapter
from aistock_agent.iterate.case_scanner import find_recent_trading_day
from aistock_agent.services.market_trace_snapshot import build_market_trace_snapshot

logger = structlog.get_logger()

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


async def source_cases(
    adapter: IterableAgentAdapter,
    *,
    data_dir: Path | None = None,
    force: bool = False,
) -> list[CaseCandidate]:
    """按 adapter.case_sources 逐个 provider 采集候选；单源失败降级跳过并告警。"""
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
    return candidates


#: provider 注册表（清单封闭：新 provider 必须登记于此）
SOURCE_PROVIDERS: dict[str, Callable[[SourceContext], Awaitable[list[CaseCandidate]]]] = {
    "market_close_snapshot": market_close_snapshot,
    "telegraph_keyword_scan": telegraph_keyword_scan,
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
    采集失败返回 None（降级，不阻断产片）。
    """
    from aistock_agent.services.data_client import NodeApiClient

    # 采集时刻用上海时区（与切片事件时间对齐；datetime.now 本地时区在服务器
    # 与容器间可能漂移，B-5 三时间戳要求可比较）
    from aistock_agent.utils.date import shanghai_now

    try:
        payload = await NodeApiClient().get_industry_graph_full()
    except Exception as exc:  # noqa: BLE001
        logger.warning("industry_graph_collect_failed", error=str(exc))
        return None
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
