"""trace_lookup Skill — 市场溯源（只读已持久化 ReviewArtifact）。

复用 services/market_trace_qa.py:load_validated_trace，跳过 LLM 选择步骤。
失败策略：报告未生成或校验失败 → degraded Evidence。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.market_trace_qa import load_validated_trace
from aistock_agent.skills.base import skill


@skill
async def trace_lookup(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    date_str = args.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)

    result = await load_validated_trace(date_str)
    if result is None:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason=f"trace report miss or invalid for {date_str}",
            skill_name="trace_lookup",
        )

    snapshot, trace = result
    # 提取事实：attribution_status + confidence + unresolved_questions
    facts: list[str] = []
    facts.append(f"归因状态: {getattr(trace, 'attribution_status', 'unknown')}")
    facts.append(f"置信度: {getattr(trace, 'confidence', 'unknown')}")
    unresolved = getattr(trace, "unresolved_questions", []) or []
    if unresolved:
        facts.append(f"未解决问题: {', '.join(unresolved[:3])}")

    # 提取因果链摘要（如有）
    candidates = getattr(trace, "candidates", []) or []
    if candidates:
        facts.append(f"候选归因数: {len(candidates)}")
        primary_chain_id = getattr(trace, "primary_chain_id", None)
        if primary_chain_id:
            facts.append(f"主因链: {primary_chain_id}")

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"trace:{date_str}",
                kind="trace",
                title=f"市场溯源 {date_str}",
                snippet=facts[0] if facts else "",
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[],
        degraded=False,
        skill_name="trace_lookup",
        raw={"date": date_str, "attribution_status": getattr(trace, "attribution_status", "")},
    )
