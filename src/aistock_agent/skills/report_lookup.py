"""report_lookup Skill — 读 DB / Redis 已持久化报告。

复用 services/cache.py：
- report_type=review → get_cached_review(date)
- report_type=morning → get_cached_briefing()（今日晨报，无 date 参数）
- report_type=chat_analysis → node_api.get_analysis_report（DB 三元组查询，
  登录态；未登录走 summary_fallback 会话内摘要，D14/D17/D38）

失败策略：缓存未命中或异常 → degraded Evidence。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.cache import get_cached_briefing, get_cached_review
from aistock_agent.skills.base import skill


def _extract_details(artifact: object) -> str:
    """从 artifact.content.display_report.details 稳健取值；缺失/非 dict 返回空串。"""
    if not isinstance(artifact, dict):
        return ""
    content = artifact.get("content")
    if not isinstance(content, dict):
        return ""
    display = content.get("display_report")
    if not isinstance(display, dict):
        return ""
    details = display.get("details")
    return str(details) if details else ""


def _extract_summary(artifact: object) -> str:
    """从 artifact.content.display_report.summary 稳健取值；缺失/非 dict 返回空串。"""
    if not isinstance(artifact, dict):
        return ""
    content = artifact.get("content")
    if not isinstance(content, dict):
        return ""
    display = content.get("display_report")
    if not isinstance(display, dict):
        return ""
    summary = display.get("summary")
    return str(summary) if summary else ""


@skill
async def report_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    report_type = args.get("report_type", "review")
    date_str = args.get("date") or datetime.now(UTC).strftime("%Y-%m-%d")
    now = datetime.now(UTC)

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

    if report_type == "chat_analysis":
        # D14/D17：三元组查询（chat_analysis, today, user_id）；report_id 不参与查询。
        # D38：未登录走 summary_fallback（会话内摘要），不读 DB。
        user_id = args.get("user_id")
        if user_id:
            from aistock_agent.services.data_client import node_api
            from aistock_agent.utils.date import shanghai_today

            artifact = await node_api.get_analysis_report(
                report_type="chat_analysis",
                report_date=args.get("date") or shanghai_today().isoformat(),
                user_id=user_id,
            )
            if artifact is None:
                return Evidence(
                    facts=[],
                    sources=[],
                    as_of=now,
                    degraded=True,
                    degraded_reason=f"chat_analysis miss for {user_id}@{date_str}",
                    skill_name="report_lookup",
                )
            details = _extract_details(artifact)   # content.display_report.details
            summary = _extract_summary(artifact)   # content.display_report.summary
            facts = [s for s in [summary, details[:400]] if s]
            return Evidence(
                facts=facts,
                sources=[
                    ChatSource(
                        source_id=f"chat_analysis:{date_str}:{user_id}",
                        kind="db_report",
                        title="上次深度分析",
                        snippet=summary or details[:100],
                        captured_at=now,
                    )
                ],
                as_of=now,
                symbols=[],
                degraded=False,
                skill_name="report_lookup",
                raw={"report_type": "chat_analysis", "date": date_str},
            )
        summary_fallback = args.get("summary_fallback")
        if summary_fallback:
            return Evidence(
                facts=[summary_fallback],
                sources=[
                    ChatSource(
                        source_id=f"chat_analysis:session:{date_str}",
                        kind="db_report",
                        title="上次深度分析（会话内）",
                        snippet=summary_fallback[:100],
                        captured_at=now,
                    )
                ],
                as_of=now,
                symbols=[],
                degraded=False,
                skill_name="report_lookup",
                raw={"report_type": "chat_analysis", "source": "session"},
            )
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="chat_analysis requires user_id or summary_fallback",
            skill_name="report_lookup",
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
