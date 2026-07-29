"""report_lookup Skill — 读 DB / Redis 已持久化报告。

复用 services/cache.py：
- report_type=review → get_cached_review(date)
- report_type=morning → get_cached_briefing()（今日晨报，无 date 参数）

失败策略：缓存未命中或异常 → degraded Evidence。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.cache import get_cached_briefing, get_cached_review
from aistock_agent.skills.base import skill


@skill
async def report_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    report_type = args.get("report_type", "review")
    date_str = args.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)

    if report_type == "review":
        artifact = await get_cached_review(date_str)
        if artifact is None:
            return Evidence(
                facts=[],
                sources=[],
                as_of=now,
                degraded=True,
                degraded_reason=f"review report miss for {date_str}",
                skill_name="report_lookup",
            )
        markdown = str(artifact.get("markdown", ""))
        trace_summary = str(artifact.get("trace_summary", ""))
        facts = [s for s in [trace_summary, markdown[:200]] if s]
        return Evidence(
            facts=facts,
            sources=[
                ChatSource(
                    source_id=f"review:{date_str}",
                    kind="db_report",
                    title=f"复盘报告 {date_str}",
                    snippet=trace_summary or markdown[:100],
                    captured_at=now,
                )
            ],
            as_of=now,
            symbols=[],
            degraded=False,
            skill_name="report_lookup",
            raw={"report_type": "review", "date": date_str},
        )

    if report_type == "morning":
        briefing = await get_cached_briefing()
        if briefing is None:
            return Evidence(
                facts=[],
                sources=[],
                as_of=now,
                degraded=True,
                degraded_reason=f"morning briefing miss for {date_str}",
                skill_name="report_lookup",
            )
        return Evidence(
            facts=[briefing[:500]],
            sources=[
                ChatSource(
                    source_id=f"morning:{date_str}",
                    kind="db_report",
                    title=f"晨报 {date_str}",
                    snippet=briefing[:100],
                    captured_at=now,
                )
            ],
            as_of=now,
            symbols=[],
            degraded=False,
            skill_name="report_lookup",
            raw={"report_type": "morning", "date": date_str},
        )

    # 不支持的 report_type
    return Evidence(
        facts=[],
        sources=[],
        as_of=now,
        degraded=True,
        degraded_reason=f"unsupported report_type: {report_type}",
        skill_name="report_lookup",
    )
