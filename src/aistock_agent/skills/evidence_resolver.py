"""evidence_resolver Skill — 只读校验市场 ReviewArtifact 证据。

共享 resolve_trace_evidence 供 trace_lookup 复用，
避免 trace_lookup 直接调用 load_validated_trace。

读取顺序：
1. get_cached_review → ReviewArtifact.model_validate → 日期一致 → 跨对象校验
2. load_validated_trace（Node 持久化）
两条合法路径都经 _trace_to_evidence 转换。
无有效工件时直接 degraded，不启动补算。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from aistock_agent.agents.workers.review import validate_trace_against_snapshot
from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.schemas.market_trace import ReviewArtifact
from aistock_agent.services.cache import get_cached_review
from aistock_agent.services.market_trace_qa import load_validated_trace
from aistock_agent.skills.base import skill

logger = structlog.get_logger()


def _trace_to_evidence(
    snapshot: Any,
    trace: Any,
    date_str: str,
    skill_name: str,
    origin: str,
    now: datetime,
) -> Evidence:
    """将已验证的 (snapshot, trace) 对转换为 Evidence。

    Args:
        snapshot: 通过校验的 MarketTraceSnapshot。
        trace: 通过校验的 MarketTraceResult。
        date_str: 请求的报告日期。
        skill_name: 调用方的 skill 名称。
        origin: 来源标识（"redis" 或 "node"）。
        now: 当前 UTC 时间。

    Returns:
        非 degraded 的 Evidence。
    """
    facts: list[str] = []
    facts.append(f"交易日: {snapshot.trade_date}")
    facts.append(f"现象状态: {snapshot.phenomenon_discovery.status}")
    facts.append(f"归因状态: {trace.attribution_status}")
    facts.append(f"置信度: {trace.confidence}")

    if snapshot.phenomenon_discovery.primary is not None:
        facts.append(f"主导现象: {snapshot.phenomenon_discovery.primary.summary}")

    candidates = trace.candidates or []
    facts.append(f"候选归因数: {len(candidates)}")

    unresolved = trace.unresolved_questions or []
    if unresolved:
        facts.append(f"未解决问题数: {len(unresolved)}")

    snippet = facts[0] if facts else ""
    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"trace:{snapshot.snapshot_id}",
                kind="trace",
                title=f"市场溯源 {snapshot.trade_date}",
                snippet=snippet,
                captured_at=snapshot.captured_at,
            )
        ],
        as_of=now,
        symbols=[],
        degraded=False,
        skill_name=skill_name,
        raw={
            "date": date_str,
            "snapshot_id": snapshot.snapshot_id,
            "origin": origin,
            "attribution_status": trace.attribution_status,
        },
    )


async def resolve_trace_evidence(date_str: str, *, skill_name: str) -> Evidence:
    """解析市场 ReviewArtifact 证据：Redis 缓存 → Node 持久化。

    顺序固定：
    1. get_cached_review → ReviewArtifact.model_validate → 日期一致 → 跨对象校验
    2. load_validated_trace
    3. 无可验证工件时直接 degraded

    Args:
        date_str: 报告日期 YYYY-MM-DD。
        skill_name: 调用方的 skill 名称（如 "trace_lookup"）。

    Returns:
        包含证据事实的 Evidence；无有效工件时 degraded=True。
    """
    now = datetime.now(UTC)

    # Path 1: Redis 缓存
    cached = await get_cached_review(date_str)
    if cached is not None:
        try:
            artifact = ReviewArtifact.model_validate(cached)
            if artifact.snapshot.trade_date == date_str:
                validate_trace_against_snapshot(artifact.trace, artifact.snapshot)
                logger.debug(
                    "evidence_resolver_redis_hit",
                    date=date_str,
                    snapshot_id=artifact.snapshot.snapshot_id,
                )
                return _trace_to_evidence(
                    artifact.snapshot,
                    artifact.trace,
                    date_str,
                    skill_name,
                    "redis",
                    now,
                )
        except Exception:
            logger.debug("evidence_resolver_redis_invalid", exc_info=True)

    # Path 2: Node 持久化
    result = await load_validated_trace(date_str)
    if result is not None:
        snapshot, trace = result
        logger.debug(
            "evidence_resolver_node_hit",
            date=date_str,
            snapshot_id=snapshot.snapshot_id,
        )
        return _trace_to_evidence(
            snapshot,
            trace,
            date_str,
            skill_name,
            "node",
            now,
        )

    # 无可验证工件 → degraded
    logger.info("evidence_resolver_miss", date=date_str)
    return Evidence(
        facts=[],
        sources=[],
        as_of=now,
        degraded=True,
        degraded_reason=f"no valid trace evidence for {date_str}",
        skill_name=skill_name,
    )


@skill
async def evidence_resolver(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    """evidence_resolver Skill — 只读市场 ReviewArtifact 证据。

    args:
        "date": 报告日期 YYYY-MM-DD（默认当前 UTC 日期）。

    Returns:
        包含证据事实的 Evidence；无可验证工件时 degraded。
    """
    date_str = args.get("date") or datetime.now(UTC).strftime("%Y-%m-%d")
    return await resolve_trace_evidence(date_str, skill_name="evidence_resolver")
