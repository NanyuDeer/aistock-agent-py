"""industry_relation Skill — 行业关系查询。

复用 tools/industry_vector_search.py 的 match_industry_by_keywords。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.tools.industry_vector_search import match_industry_by_keywords


@skill
async def industry_relation(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    keywords = args.get("keywords") or []
    if not keywords and goal.tag_codes:
        keywords = list(goal.tag_codes)
    if not keywords:
        raise ValueError("industry_relation requires 'keywords' in args or goal.tag_codes")

    result_text = await match_industry_by_keywords.ainvoke({"keywords": keywords})
    now = datetime.now(UTC)

    facts = [line.strip() for line in result_text.splitlines() if line.strip()]
    if not facts:
        facts = [result_text]

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"industry:{':'.join(keywords)}:{now.isoformat()}",
                kind="industry",
                title=f"行业关系 {','.join(keywords)}",
                snippet=result_text[:200],
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[],
        degraded=False,
        skill_name="industry_relation",
        raw={"keywords": keywords},
    )
