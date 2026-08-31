"""stock_trace_lookup Skill — 自选股异动溯源（只读价格异动/涨停雷达归因结果）。

阶段 2.2：对话内可查登录用户自选股的异动溯源（stock_trace 链路：午尾盘价格异动
mv 事件 + 涨停雷达触发，五层候选归因 primary_cause）。
走 Node internal 只读端点（openid 过滤 user_stocks 归属），不触发任何写操作。
入参 {symbol?: "6位代码"}，无 symbol 返回用户自选股全部异动溯源，有 symbol 只查单只。
user_id（openid）由 qa_router postprocess 确定性注入（登录态）。
失败策略：未登录 / 无数据 / 调用异常 → degraded Evidence（对齐 insight_lookup）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill


def _event_fact(row: dict[str, Any]) -> str:
    """单条溯源事件 → fact 行：{股票名(代码)} 异动({direction})：{主因/状态}。"""
    name = str(row.get("stock_name") or "")
    symbol = str(row.get("symbol") or "")
    direction = str(row.get("direction") or "")
    cause = str(row.get("primary_cause") or "")
    status = str(row.get("analysis_status") or "processing")
    head = f"{name}({symbol})" if name else symbol
    direction_txt = "上涨" if direction == "up" else "下跌" if direction == "down" else direction
    if cause:
        return f"{head} 异动{direction_txt}归因：{cause}"
    if status == "processing":
        return f"{head} 异动{direction_txt}（归因分析中）"
    return f"{head} 异动{direction_txt}（暂无归因结论）"


@skill
async def stock_trace_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    user_id = args.get("user_id")
    now = datetime.now(UTC)
    if not user_id:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="stock_trace_lookup requires login (user_id)",
            skill_name="stock_trace_lookup",
        )

    from aistock_agent.services.data_client import node_api

    symbol = args.get("symbol")
    try:
        rows = await node_api.list_stock_traces(
            str(user_id),
            symbol=str(symbol) if symbol else None,
        )
    except Exception as exc:  # noqa: BLE001 —— skill 失败策略：异常降级不抛
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason=f"stock_trace_lookup failed: {exc}",
            skill_name="stock_trace_lookup",
        )
    if not rows:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="no stock trace events for user",
            skill_name="stock_trace_lookup",
        )

    facts = [_event_fact(r) for r in rows]
    sources = [
        ChatSource(
            source_id=f"stock_trace:{r.get('event_id')}",
            kind="stock_trace",
            title=f"{r.get('stock_name') or r.get('symbol')} 异动溯源",
            snippet=str(r.get("primary_cause") or _event_fact(r))[:100],
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
        skill_name="stock_trace_lookup",
        raw={"count": len(rows)},
    )
