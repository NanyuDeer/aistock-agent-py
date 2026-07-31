"""trace_lookup Skill — 市场溯源（只读已持久化 ReviewArtifact）。

复用 evidence_resolver 的 resolve_trace_evidence 共享 helper。
保留 skill_name="trace_lookup"、ChatSource.kind="trace"。
失败策略：报告未生成或校验失败 → degraded Evidence。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.skills.evidence_resolver import resolve_trace_evidence


@skill
async def trace_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    date_str = args.get("date") or datetime.now(UTC).strftime("%Y-%m-%d")
    return await resolve_trace_evidence(date_str, skill_name="trace_lookup")
