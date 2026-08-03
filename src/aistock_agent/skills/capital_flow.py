"""capital_flow Skill — 个股资金流向。

复用 tools/stock_tools.py 的 get_capital_flow。非交易时段 / 数据源未返回 → degraded=True，facts 含时段提示。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_capital_flow
from aistock_agent.utils.date import trading_session_status

_EMPTY_MARKERS = ("未找到股票", "资金流向数据为空", "数据不可用")


@skill
async def capital_flow(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("capital_flow requires 'symbol' in args or goal.symbols")

    flow_text = await get_capital_flow.ainvoke({"symbol": symbol})
    now = datetime.now(UTC)

    status, hint = trading_session_status()
    is_empty = any(marker in flow_text for marker in _EMPTY_MARKERS)

    if is_empty:
        degraded = True
        reason = f"数据源未返回（{status}）"
        facts = [f"当前为{hint}，{symbol} 资金流向数据暂未返回。"]
    elif status != "trading":
        degraded = True
        reason = f"非交易时段（{status}）"
        facts = [f"{hint}。以下为最近交易日数据：\n{flow_text}"]
    else:
        degraded = False
        reason = ""
        facts = [flow_text]

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"flow:{symbol}:{now.isoformat()}",
                kind="capital_flow",
                title=f"{symbol} 资金流向",
                snippet=facts[0][:200],
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=degraded,
        degraded_reason=reason,
        skill_name="capital_flow",
        raw={"symbol": symbol, "trading_status": status},
    )
