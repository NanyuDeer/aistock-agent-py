"""index_snapshot Skill（P5 工作线 B）— A 股指数快速快照（腾讯源+缓存，几百 ms）。

绕开 market_snapshot 的 quick 全市场爬取慢路径（eastmoneyThrottler 300ms × 111 批 ≈ 33s
> agent-py 10s 超时 → 必回退 last-close）。仅回答"指数现价"，不回答"全市场宽度"。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.base import skill

DEFAULT_SYMBOLS = ("000001", "399001", "399006")


@skill
async def index_snapshot(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    raw_symbols = args.get("symbols") or []
    symbols = [s for s in raw_symbols if isinstance(s, str) and s.isdigit() and len(s) == 6]
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)
    symbols = list(dict.fromkeys(symbols))[:10]

    data = await node_api.get("/internal/index/quotes?symbols=" + ",".join(symbols))
    now = datetime.now(UTC)
    indices: list[dict] = []
    if isinstance(data, dict):
        indices = [i for i in data.get("indices", []) if isinstance(i, dict)]

    facts: list[str] = []
    sources: list[ChatSource] = []
    failed = 0
    for idx in indices:
        name = idx.get("name") or idx.get("index") or "-"
        code = idx.get("index") or "-"
        price = idx.get("price")
        pct = idx.get("changePercent")
        if price is None or pct is None:
            facts.append(f"{name}({code})：该指数暂无数据")
            continue
        if isinstance(pct, int | float):
            facts.append(f"{name}({code}) 最新价 {price} 涨跌幅 {pct:+.2f}%")
        else:
            facts.append(f"{name}({code}) 最新价 {price}")
        sources.append(
            ChatSource(
                source_id=f"index:{code}:{now.isoformat()}",
                kind="realtime_quote",
                title=f"{name}({code})",
                snippet=facts[-1][:200],
                captured_at=now,
            )
        )
    if not indices:
        failed = 1

    degraded = failed > 0
    if degraded and not facts:
        facts = ["今日指数数据暂不可用。"]

    return Evidence(
        facts=facts,
        sources=sources,
        as_of=now,
        symbols=[i.get("index") or "" for i in indices],
        degraded=degraded,
        degraded_reason="index quotes unavailable" if degraded else None,
        skill_name="index_snapshot",
        raw={"indices": indices, "source": "index_quotes"},
    )
