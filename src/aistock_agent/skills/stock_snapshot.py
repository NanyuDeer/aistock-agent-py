"""stock_snapshot Skill — 实时个股行情。

复用 tools/stock_tools.py 的 get_quote。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_quote


@skill
async def stock_snapshot(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("stock_snapshot requires 'symbol' in args or goal.symbols")

    quote_text = await get_quote.ainvoke({"symbol": symbol})
    now = datetime.now(UTC)

    return Evidence(
        facts=[quote_text],
        sources=[
            ChatSource(
                source_id=f"quote:{symbol}:{now.isoformat()}",
                kind="realtime_quote",
                title=f"{symbol} 实时行情",
                snippet=quote_text[:200],
                occurred_at=now,
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=False,
        skill_name="stock_snapshot",
        raw={"symbol": symbol},
    )
