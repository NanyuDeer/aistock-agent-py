"""market_snapshot Skill — 当前 A 股与全球市场快照。

仅读 Node 当前 A 股 quick/full 快照与已有 collect_global_market_facts，
不接受历史 date 参数，不调用 LLM、review、build_market_trace_snapshot。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.services.market_trace_snapshot import normalize_a_share
from aistock_agent.skills.base import skill
from aistock_agent.tools.market_tools import collect_global_market_facts

_VALID_SCOPES = frozenset({"a_share", "global", "both"})
_VALID_KINDS = frozenset({"quick", "full"})


def _date_label(trade_date: object) -> str | None:
    """YYYYMMDD → 'MM-DD' 展示标签；格式异常返回 None（防御：不拼日期、不崩溃）。"""
    if isinstance(trade_date, str) and len(trade_date) == 8 and trade_date.isdigit():
        return f"{trade_date[4:6]}-{trade_date[6:8]}"
    return None


def _build_a_share_facts(normalized: dict[str, Any], trade_date: object = "") -> list[str]:
    """从归一化的 A 股数据中提取可读 facts（始终带交易日，防止 LLM 误标"今日"）。"""
    facts: list[str] = []
    date_label = _date_label(trade_date)
    if date_label:
        # 锚点行：覆盖成交额/涨跌停/板块等其余不带日期的行
        facts.append(f"数据日期：{date_label}")

    # 指数
    indexes_map = normalized.get("indexes")
    if isinstance(indexes_map, dict):
        for idx in indexes_map.values():
            if not isinstance(idx, dict):
                continue
            name = idx.get("name", "")
            display = f"{name}({date_label})" if date_label else name
            close = idx.get("close", "")
            change_pct = idx.get("change_pct")
            if change_pct is not None:
                facts.append(f"{display}: {close} ({change_pct:+.2f}%)")
            elif close:
                facts.append(f"{display}: {close}")

    # 市场广度
    breadth = normalized.get("breadth")
    if isinstance(breadth, dict):
        total = breadth.get("total_count")
        advance = breadth.get("advance_count")
        decline = breadth.get("decline_count")
        if total:
            facts.append(
                f"涨跌家数: 涨{advance} 跌{decline} "
                f"平{breadth.get('flat_count', 0)} 共{total}"
            )

    # 成交额
    turnover = normalized.get("turnover")
    if isinstance(turnover, dict):
        amount = turnover.get("amount_yuan")
        change_pct = turnover.get("change_pct")
        if amount is not None:
            change_str = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
            facts.append(f"成交额: {amount}{change_str}")

    # 涨跌停
    limits = normalized.get("limits")
    if isinstance(limits, dict):
        up = limits.get("up_count")
        down = limits.get("down_count")
        broken = limits.get("broken_count")
        board = limits.get("highest_board")
        if up is not None:
            facts.append(f"涨停{up} 跌停{down} 炸板{broken} 最高连板{board}")

    # 主力资金
    main_force = normalized.get("main_force")
    if isinstance(main_force, dict):
        net = main_force.get("large_and_extra_large_net_yuan")
        if net is not None:
            facts.append(f"主力净额: {net}")

    # 板块（简单提及最强/最弱）
    sectors = normalized.get("sectors")
    if isinstance(sectors, dict):
        gainers = sectors.get("top_gainers", [])
        losers = sectors.get("top_losers", [])
        if isinstance(gainers, list) and gainers:
            top_name = gainers[0].get("name", "") if isinstance(gainers[0], dict) else ""
            if top_name:
                facts.append(f"最强板块: {top_name}")
        if isinstance(losers, list) and losers:
            top_name = losers[0].get("name", "") if isinstance(losers[0], dict) else ""
            if top_name:
                facts.append(f"最弱板块: {top_name}")

    return facts


def _safe_str(value: object) -> str:
    return str(value) if value is not None else ""


def _build_a_share_source(
    raw_data: dict[str, Any],
    captured_at: datetime,
    *,
    used_last_close: bool = False,
) -> ChatSource:
    """构建 A 股 ChatSource（kind=realtime_quote）。

    used_last_close=True 表示数据来自最近交易日（非交易日降级回退），
    title 标注数据日期，便于用户与综合回答模型区分"今天"与"最近交易日"。
    """
    trade_date = raw_data.get("trade_date", "")
    snapshot_kind = raw_data.get("snapshot_kind", "full")
    snippet_parts: list[str] = []
    indexes_raw = raw_data.get("indexes")
    if isinstance(indexes_raw, list):
        for idx in indexes_raw[:3]:
            if isinstance(idx, dict):
                name = idx.get("name", "")
                close = idx.get("close", "")
                snippet_parts.append(f"{name}={close}")
    snippet = " ".join(snippet_parts) if snippet_parts else f"A股{snapshot_kind}快照"
    source_id = f"market:a_share:{snapshot_kind}:{trade_date}"
    if used_last_close:
        title = f"A 股最近交易日快照 ({trade_date})"
    else:
        title = f"A 股 {snapshot_kind} 快照 ({trade_date})"
    return ChatSource(
        source_id=source_id,
        kind="realtime_quote",
        title=title,
        snippet=snippet,
        captured_at=captured_at,
    )


def _build_global_facts(global_facts: list[dict[str, Any]]) -> list[str]:
    """从全球 market facts 中提取可读 facts。"""
    facts: list[str] = []
    for fact in global_facts:
        if not isinstance(fact, dict):
            continue
        name = fact.get("name", fact.get("ticker", ""))
        price = fact.get("price")
        change_pct = fact.get("change_pct")
        if price is not None:
            change_str = f" ({change_pct:+.2f}%)" if change_pct is not None else ""
            facts.append(f"{name}: {price}{change_str}")
    return facts


def _build_global_source(
    global_facts: list[dict[str, Any]],
    captured_at: datetime,
) -> ChatSource:
    """构建全球 ChatSource（kind=realtime_quote）。"""
    snippet_parts: list[str] = []
    for fact in global_facts[:3]:
        if isinstance(fact, dict):
            name = fact.get("name", "")
            price = fact.get("price", "")
            snippet_parts.append(f"{name}={price}")
    snippet = " ".join(snippet_parts) if snippet_parts else "全球行情"
    return ChatSource(
        source_id=f"market:global:{captured_at.strftime('%Y%m%dT%H%M%S')}",
        kind="realtime_quote",
        title="全球市场行情",
        snippet=snippet,
        captured_at=captured_at,
    )


@skill
async def market_snapshot(args: dict[str, Any], goal: InsightGoal) -> Evidence:  # noqa: ARG001
    """当前 A 股与全球市场快照。

    Args:
        args:
            scope: "a_share" / "global" / "both"（默认 "both"）
            snapshot_kind: "quick" / "full"（默认 "quick"）
        goal: InsightGoal（本 skill 不使用它）
    """
    scope: str = args.get("scope", "both")
    snapshot_kind: str = args.get("snapshot_kind", "quick")

    # ── 参数校验（在发起任何请求之前）──
    degraded_reasons: list[str] = []
    if scope not in _VALID_SCOPES:
        degraded_reasons.append(f"无效 scope: {scope!r}")
    if snapshot_kind not in _VALID_KINDS:
        degraded_reasons.append(f"无效 snapshot_kind: {snapshot_kind!r}")

    if degraded_reasons:
        raise ValueError("; ".join(degraded_reasons))

    captured_at = datetime.now(UTC)
    all_facts: list[str] = []
    all_sources: list[ChatSource] = []
    a_share_success = False
    global_success = False
    # 记录 A 股是否来自 last-close 降级回退（供 Evidence.raw 暴露）
    a_share_meta: dict[str, object] = {}

    # ── 内部协程：获取 A 股 ──
    async def _fetch_a_share() -> tuple[str, list[str], list[ChatSource], bool]:
        local_facts: list[str] = []
        local_sources: list[ChatSource] = []
        try:
            a_share_raw: dict[str, Any] | None = None
            used_last_close = False
            if snapshot_kind == "quick":
                a_share_raw = await node_api.get_quick_snapshot()
                if a_share_raw is None:
                    # 非交易日/盘前：回退最近已完成交易日收盘快照（复用 market_trace 降级先例）
                    a_share_raw = await node_api.get_last_close_snapshot()
                    used_last_close = True
            else:
                a_share_raw = await node_api.get("/internal/market/close-snapshot")
                if a_share_raw is None:
                    a_share_raw = await node_api.get_last_close_snapshot()
                    used_last_close = True

            if a_share_raw is None:
                degraded_reasons.append("A 股当前与最近交易日数据均不可用（Node 返回 None）")
                return "a_share", local_facts, local_sources, False

            # Full/last-close 快照需要双重 coverage 校验（quick 不校验）
            if snapshot_kind == "full" or used_last_close:
                coverage = a_share_raw.get("coverage")
                coverage_dict = coverage if isinstance(coverage, dict) else {}
                current_daily = coverage_dict.get("current_daily")
                current_daily_dict = (
                    current_daily if isinstance(current_daily, dict) else {}
                )
                previous_daily = coverage_dict.get("previous_daily")
                previous_daily_dict = (
                    previous_daily if isinstance(previous_daily, dict) else {}
                )
                if current_daily_dict.get("complete") is not True:
                    degraded_reasons.append("A 股当前交易日 coverage 不完整")
                    return "a_share", local_facts, local_sources, False
                if previous_daily_dict.get("complete") is not True:
                    degraded_reasons.append("A 股前交易日 coverage 不完整")
                    return "a_share", local_facts, local_sources, False

            normalized = normalize_a_share(a_share_raw)
            local_facts.extend(_build_a_share_facts(normalized, a_share_raw.get("trade_date")))
            local_sources.append(
                _build_a_share_source(
                    a_share_raw, captured_at, used_last_close=used_last_close
                )
            )
            if used_last_close:
                a_share_meta["used_last_close"] = True
                a_share_meta["trade_date"] = _safe_str(a_share_raw.get("trade_date"))
            return "a_share", local_facts, local_sources, True
        except Exception as exc:
            degraded_reasons.append(f"A 股数据获取异常: {exc}")
            return "a_share", local_facts, local_sources, False

    # ── 内部协程：获取全球市场 ──
    async def _fetch_global() -> tuple[str, list[str], list[ChatSource], bool]:
        local_facts: list[str] = []
        local_sources: list[ChatSource] = []
        try:
            global_facts = await asyncio.to_thread(
                collect_global_market_facts, captured_at
            )
            if global_facts:
                local_facts.extend(_build_global_facts(global_facts))
                local_sources.append(
                    _build_global_source(global_facts, captured_at)
                )
                return "global", local_facts, local_sources, True

            degraded_reasons.append(
                "全球市场数据为空（collect_global_market_facts 返回空列表）"
            )
            return "global", local_facts, local_sources, False
        except Exception as exc:
            degraded_reasons.append(f"全球市场数据获取异常: {exc}")
            return "global", local_facts, local_sources, False

    # ── 并发获取 ──
    coros: list[asyncio.Task[tuple[str, list[str], list[ChatSource], bool]]] = []
    if scope in ("a_share", "both"):
        coros.append(asyncio.create_task(_fetch_a_share()))  # type: ignore[arg-type]
    if scope in ("global", "both"):
        coros.append(asyncio.create_task(_fetch_global()))  # type: ignore[arg-type]

    results = await asyncio.gather(*coros, return_exceptions=True)

    for result in results:
        if isinstance(result, BaseException):
            degraded_reasons.append(f"快照获取异常: {result}")
            continue
        name, facts, sources, success = result
        all_facts.extend(facts)
        all_sources.extend(sources)
        if name == "a_share" and success:
            a_share_success = True
        if name == "global" and success:
            global_success = True

    degraded = len(degraded_reasons) > 0
    degraded_reason = "; ".join(degraded_reasons) if degraded_reasons else None

    return Evidence(
        facts=all_facts,
        sources=all_sources,
        as_of=captured_at,
        degraded=degraded,
        degraded_reason=degraded_reason,
        skill_name="market_snapshot",
        raw={
            "scope": scope,
            "snapshot_kind": snapshot_kind,
            "a_share_success": a_share_success,
            "global_success": global_success,
            **a_share_meta,
        },
    )
