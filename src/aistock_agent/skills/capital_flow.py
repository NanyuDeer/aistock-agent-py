"""capital_flow Skill — 个股资金流向。

复用 tools/stock_tools.py 的 get_capital_flow。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_capital_flow


@skill
async def capital_flow(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("capital_flow requires 'symbol' in args or goal.symbols")

    flow_text = await get_capital_flow.ainvoke({"symbol": symbol})
    now = datetime.now(UTC)

    return Evidence(
        facts=[flow_text],
        sources=[
            ChatSource(
                source_id=f"flow:{symbol}:{now.isoformat()}",
                kind="capital_flow",
                title=f"{symbol} 资金流向",
                snippet=flow_text[:200],
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=False,
        skill_name="capital_flow",
        raw={"symbol": symbol},
    )
