"""stock_news Skill — 个股资讯。

复用 tools/news_tools.py 的 search_cls_news。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.news_tools import search_cls_news


@skill
async def stock_news(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("stock_news requires 'symbol' in args or goal.symbols")

    news_text = await search_cls_news(symbol)
    now = datetime.now(timezone.utc)

    # search_cls_news 返回多行文本，按行拆为 facts
    facts = [line.strip() for line in news_text.splitlines() if line.strip()]
    if not facts:
        facts = [news_text]

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"news:{symbol}:{now.isoformat()}",
                kind="news",
                title=f"{symbol} 财联社资讯",
                snippet=news_text[:200],
                occurred_at=now,
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=False,
        skill_name="stock_news",
        raw={"symbol": symbol},
    )
