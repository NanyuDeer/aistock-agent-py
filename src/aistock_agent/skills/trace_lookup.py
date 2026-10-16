"""trace_lookup Skill — 市场溯源（只读已持久化 ReviewArtifact）。

复用 evidence_resolver 的 resolve_trace_evidence 共享 helper。
保留 skill_name="trace_lookup"、ChatSource.kind="trace"。
失败策略：报告未生成或校验失败 → degraded Evidence。
"""
from __future__ import annotations

from typing import Any

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.skills.evidence_resolver import resolve_trace_evidence
from aistock_agent.utils.date import shanghai_today


@skill
async def trace_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    # 默认报告日期取**上海自然日**（勿用 datetime.now(UTC)：UTC 日在京时 00:00–08:00
    # 期间会落到前一天，用户此刻问"今天的溯源"会查到昨天）。
    date_str = args.get("date") or shanghai_today().isoformat()
    return await resolve_trace_evidence(date_str, skill_name="trace_lookup")
