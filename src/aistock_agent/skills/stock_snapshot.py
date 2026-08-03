"""stock_snapshot Skill — 实时个股行情。

复用 tools/stock_tools.py 的 get_quote。非交易时段 / 数据源未返回 → degraded=True，facts 含时段提示。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_quote
from aistock_agent.utils.date import trading_session_status

# get_quote / get_capital_flow 在数据为空时返回的固定字样
_EMPTY_MARKERS = ("未找到股票", "行情数据为空", "数据不可用")


@skill
async def stock_snapshot(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("stock_snapshot requires 'symbol' in args or goal.symbols")

    quote_text = await get_quote.ainvoke({"symbol": symbol})
    now = datetime.now(UTC)

    # 判断数据有效性 + 交易时段
    status, hint = trading_session_status()
    is_empty = any(marker in quote_text for marker in _EMPTY_MARKERS)

    if is_empty:
        degraded = True
        reason = f"数据源未返回（{status}）"
        facts = [f"当前为{hint}，{symbol} 实时行情暂未返回。"]
    elif status != "trading":
        degraded = True
        reason = f"非交易时段（{status}）"
        facts = [f"{hint}。以下为最近交易日数据：\n{quote_text}"]
    else:
        degraded = False
        reason = ""
        facts = [quote_text]

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"quote:{symbol}:{now.isoformat()}",
                kind="realtime_quote",
                title=f"{symbol} 实时行情",
                snippet=facts[0][:200],
                occurred_at=now,
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=degraded,
        degraded_reason=reason,
        skill_name="stock_snapshot",
        raw={"symbol": symbol, "trading_status": status},
    )
