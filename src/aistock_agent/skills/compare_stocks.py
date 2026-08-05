"""compare_stocks Skill（D40）— 多标的对比，零 Node 改动。

asyncio.gather 并发调 get_quote（适配器）聚合为单条 Evidence。
仅个股语义（spec §2.6）：get_quote 无指数数据源，指数目标由 qa_router 白名单移除。
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_quote

MAX_COMPARE_SYMBOLS = 5

# get_quote 输出格式：【名称】最新价: X  涨跌幅: Y%
_QUOTE_RE = re.compile(
    r"【(?P<name>.+?)】最新价: (?P<price>\S+)  涨跌幅: (?P<pct>[+-]?\d+(?:\.\d+)?)%"
)

_EMPTY_MARKERS = ("未找到股票", "数据暂不可用")


@skill
async def compare_stocks(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbols = list(dict.fromkeys(args.get("symbols") or goal.symbols or []))
    if len(symbols) < 2:
        raise ValueError("compare_stocks requires at least 2 symbols")
    symbols = symbols[:MAX_COMPARE_SYMBOLS]

    results = await asyncio.gather(
        *(get_quote.ainvoke({"symbol": s}) for s in symbols),
        return_exceptions=True,
    )

    now = datetime.now(UTC)
    facts: list[str] = []
    sources: list[ChatSource] = []
    failed: list[str] = []
    parsed: list[dict[str, object]] = []

    for symbol, raw in zip(symbols, results, strict=True):
        if isinstance(raw, BaseException) or any(m in str(raw) for m in _EMPTY_MARKERS):
            failed.append(symbol)
            # P11（线 3）：失败标的标 available=False，无价格字段（spec §3.1）
            parsed.append({"name": symbol, "code": symbol, "available": False})
            facts.append(f"{symbol}：数据暂不可用")
            continue
        text = str(raw)
        m = _QUOTE_RE.search(text)
        name = m.group("name") if m else symbol
        facts.append(f"{name}({symbol}): {text}")
        sources.append(
            ChatSource(
                source_id=f"compare:{symbol}:{now.isoformat()}",
                kind="realtime_quote",
                title=f"{name}({symbol}) 实时行情",
                snippet=text[:200],
                captured_at=now,
            )
        )
        if m:
            try:
                parsed.append({
                    "name": name,
                    "code": symbol,
                    "price": float(m.group("price")),
                    "change_pct": float(m.group("pct")),
                    "available": True,
                })
            except ValueError:
                pass  # price/pct 文本非常规数值 → 不进入 parsed（与旧逻辑 float 失败跳过一致）

    success = [
        p for p in parsed
        if p.get("available") is True and isinstance(p.get("change_pct"), int | float)
    ]
    if len(success) >= 2:
        best = max(success, key=lambda p: float(p["change_pct"]))
        worst = min(success, key=lambda p: float(p["change_pct"]))
        facts.append(
            f"对比结论：{best['name']} 涨幅最高（{best['change_pct']:+.2f}%），"
            f"{worst['name']} 涨幅最低（{worst['change_pct']:+.2f}%）"
        )

    degraded = bool(failed)
    return Evidence(
        facts=facts,
        sources=sources,
        as_of=now,
        symbols=symbols,
        degraded=degraded,
        degraded_reason="部分标的行情不可用" if degraded else None,
        skill_name="compare_stocks",
        raw={"quotes": [f for f in facts], "compared": symbols, "failed": failed,
             "parsed": parsed},
    )
