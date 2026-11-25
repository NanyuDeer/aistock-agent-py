"""insight_lookup Skill — 自选股洞察（只读涨停雷达/价格异动归因结果）。

阶段 2.1：对话内可查登录用户自选股的洞察归因（涨停雷达/午尾盘价格异动）。
走 Node internal 只读端点（openid 过滤 user_stocks 归属），不触发任何写操作。
入参 {symbol?: "6位代码"}，无 symbol 返回用户自选股全部洞察，有 symbol 只查单只。
user_id（openid）由 qa_router postprocess 确定性注入（登录态）。
失败策略：未登录 / 无数据 / 调用异常 → degraded Evidence（对齐 trace_lookup）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill


def _driver_text(driver: object) -> str:
    """从 primary_driver（dict {label/category/confidence/evidence_quote}）取展示文本。"""
    if isinstance(driver, dict):
        return str(driver.get("label") or driver.get("summary") or "")
    return str(driver) if driver else ""


def _event_fact(row: dict[str, Any]) -> str:
    """单条洞察事件 → fact 行：{股票名(代码)} {event_type}/{direction} 归因：{主因}。"""
    name = str(row.get("stock_name") or "")
    symbol = str(row.get("symbol") or "")
    event_type = str(row.get("event_type") or "")
    direction = str(row.get("direction") or "")
    primary = _driver_text(row.get("primary_driver"))
    display = row.get("display_report")
    summary = str(display.get("summary")) if isinstance(display, dict) else ""
    body = primary or summary
    head = f"{name}({symbol})" if name else symbol
    if body:
        return f"{head} {event_type}/{direction} 归因：{body}"
    return f"{head} {event_type}/{direction}（暂无归因结论）"


@skill
async def insight_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    user_id = args.get("user_id")
    now = datetime.now(UTC)
    if not user_id:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="insight_lookup requires login (user_id)",
            skill_name="insight_lookup",
        )

    from aistock_agent.services.data_client import node_api

    symbol = args.get("symbol")
    try:
        rows = await node_api.list_insights(
            str(user_id),
            symbol=str(symbol) if symbol else None,
        )
    except Exception as exc:  # noqa: BLE001 —— skill 失败策略：异常降级不抛
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason=f"insight_lookup failed: {exc}",
            skill_name="insight_lookup",
        )
    if not rows:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="no insight events for user",
            skill_name="insight_lookup",
        )

    facts = [_event_fact(r) for r in rows]
    sources = [
        ChatSource(
            source_id=f"insight:{r.get('event_id')}",
            kind="insight",
            title=f"{r.get('stock_name') or r.get('symbol')} {r.get('event_type')} 洞察",
            snippet=(_driver_text(r.get("primary_driver")) or _event_fact(r))[:100],
            captured_at=now,
        )
        for r in rows
    ]
    symbols = [str(r["symbol"]) for r in rows if r.get("symbol")]
    return Evidence(
        facts=facts,
        sources=sources,
        as_of=now,
        symbols=symbols,
        degraded=False,
        skill_name="insight_lookup",
        raw={"count": len(rows)},
    )
