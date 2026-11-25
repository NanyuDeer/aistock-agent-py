"""stock_history Skill（D41）— 个股日 K 线区间（经 Node /internal/quote/:symbol/kline）。

facts 含区间涨跌幅/最高最低/最近 5 日明细，控制 token；空 rows → degraded。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.base import skill

MAX_DAYS = 120
RECENT_SHOW = 5


def _pct(rows: list[dict[str, Any]]) -> float | None:
    try:
        first = float(rows[-1]["close"])
        last = float(rows[0]["close"])
        if first == 0:
            return None
        return (last - first) / first * 100.0
    except (KeyError, TypeError, ValueError, IndexError):
        return None


@skill
async def stock_history(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("stock_history requires 'symbol' in args or goal.symbols")
    try:
        days = min(max(int(args.get("days") or 30), 1), MAX_DAYS)
    except (TypeError, ValueError):
        days = 30

    data = await node_api.get(f"/internal/quote/{symbol}/kline?days={days}&klt=101&fqt=1")
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        rows_raw = data.get("rows")
        if isinstance(rows_raw, list):
            rows = [r for r in rows_raw if isinstance(r, dict)]

    if not rows:
        return Evidence(
            facts=[f"{symbol} 近 {days} 日 K 线数据暂不可用。"],
            sources=[],
            as_of=now,
            symbols=[symbol],
            degraded=True,
            degraded_reason="kline rows empty",
            skill_name="stock_history",
            raw={"symbol": symbol, "klt": 101, "days": 0, "rows": []},
        )

    interval_pct = _pct(rows)
    first_close = rows[-1].get("close")
    last_close = rows[0].get("close")
    highs = [h for h in (r.get("high") for r in rows) if isinstance(h, int | float)]
    lows = [lo for lo in (r.get("low") for r in rows) if isinstance(lo, int | float)]

    facts = [
        f"{symbol} 近 {len(rows)} 日走势：期初收盘 {first_close}，期末收盘 {last_close}"
        + (f"，区间涨跌幅 {interval_pct:+.2f}%" if interval_pct is not None else ""),
        f"区间最高 {max(highs) if highs else '—'}，最低 {min(lows) if lows else '—'}",
    ]
    recent = rows[:RECENT_SHOW]
    facts.append("最近明细：")
    for r in reversed(recent):
        facts.append(
            f"  {r.get('trade_date', '')} 收盘 {r.get('close')} 涨跌幅 {r.get('pct_chg')}%"
        )

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"history:{symbol}:{now.isoformat()}",
                kind="realtime_quote",
                title=f"{symbol} 近 {len(rows)} 日K线",
                snippet=facts[0][:200],
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=False,
        skill_name="stock_history",
        raw={"symbol": symbol, "klt": 101, "days": len(rows), "rows": rows},
    )
