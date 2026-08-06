"""trend_ranking Skill（D42）— 趋势股评分 Top 榜（零 Node 改动，/internal/trend/top 已存在）。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.base import skill

MAX_LIMIT = 50


@skill
async def trend_ranking(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    try:
        limit = min(max(int(args.get("limit") or 20), 1), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = 20

    data = await node_api.get_list(f"/internal/trend/top?limit={limit}")
    now = datetime.now(UTC)

    items = data if isinstance(data, list) else []
    if not items:
        return Evidence(
            facts=["暂无可用的趋势股榜单（非交易时段或榜单未生成）。"],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="trend top empty",
            skill_name="trend_ranking",
            raw={"items": [], "limit": limit},
        )

    facts = []
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("symbol") or "-"
        symbol = it.get("symbol") or "-"
        score = it.get("score")
        label = it.get("label") or "-"
        industry = it.get("industry") or ""
        facts.append(f"{i}. {name}({symbol}) {score}分({label}) {industry}".rstrip())

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"trend_top:{now.isoformat()}",
                kind="realtime_quote",
                title="趋势股评分 Top 榜",
                snippet=facts[0][:200] if facts else "",
                captured_at=now,
            )
        ],
        as_of=now,
        degraded=False,
        skill_name="trend_ranking",
        raw={"items": items, "limit": limit},
    )
