"""skill_executor 节点 — 按 skill_calls 拓扑排序执行 Skills，收集 Evidence。

direct 视为长度=1 的 compose 特例。无依赖并行，有依赖串行。
单 Skill 异常被 @skill 装饰器捕获为 degraded Evidence，不中断。
"""
from __future__ import annotations

import asyncio
from datetime import UTC
from typing import Any

import structlog

from aistock_agent.schemas.chat_contract import Evidence, SkillCall
from aistock_agent.skills.base import skill
from aistock_agent.skills.capital_flow import capital_flow
from aistock_agent.skills.evidence_resolver import evidence_resolver
from aistock_agent.skills.industry_relation import industry_relation
from aistock_agent.skills.market_snapshot import market_snapshot
from aistock_agent.skills.report_lookup import report_lookup
from aistock_agent.skills.sector_snapshot import sector_snapshot
from aistock_agent.skills.stock_news import stock_news
from aistock_agent.skills.stock_snapshot import stock_snapshot
from aistock_agent.skills.trace_lookup import trace_lookup
from aistock_agent.state.chat_schema import QuestionState

logger = structlog.get_logger()

# Skill 注册表：skill_name → 可调用对象
SKILL_REGISTRY: dict[str, Any] = {
    "capital_flow": capital_flow,
    "evidence_resolver": evidence_resolver,
    "industry_relation": industry_relation,
    "market_snapshot": market_snapshot,
    "report_lookup": report_lookup,
    "sector_snapshot": sector_snapshot,
    "stock_news": stock_news,
    "stock_snapshot": stock_snapshot,
    "trace_lookup": trace_lookup,
}


async def _execute_skill_safe(
    skill_call: SkillCall, goal: Any
) -> Evidence:
    """执行单个 Skill，确保异常被捕获为 degraded Evidence。

    SKILL_REGISTRY 中的 Skill 已经被 @skill 装饰，但为防御性编程，
    这里再包一层 try-except（应对 Skill 注册时未装饰的情况）。
    """
    skill_func = SKILL_REGISTRY.get(skill_call.skill_name)
    if skill_func is None:
        from datetime import datetime

        return Evidence(
            facts=[],
            sources=[],
            as_of=datetime.now(UTC),
            degraded=True,
            degraded_reason=f"skill not registered: {skill_call.skill_name}",
            skill_name=skill_call.skill_name,
        )

    # 若注册的是裸函数，用 @skill 再包一层；若已被 @skill 装饰，再包一层也不会出错
    # （@skill 内部已处理异常，外层 try 不会触发）
    try:
        return await skill_func(skill_call.args, goal)
    except Exception:
        # 走到这里说明 SKILL_REGISTRY 注册的是裸函数（未装饰）
        # 用 @skill 临时包装后重试
        decorated = skill(skill_func)
        ev = await decorated(skill_call.args, goal)
        # @skill 装饰器使用 func.__name__ 设置 skill_name，可能与注册名不同
        # 覆盖为 SkillCall 中的注册 skill_name
        ev.skill_name = skill_call.skill_name
        return ev


def _topo_sort(skill_calls: list[SkillCall]) -> list[list[SkillCall]]:
    """拓扑分组：同一组内无依赖可并行，组间串行。

    MVP 简化实现：depends_on 为空的归入第 0 组（并行），
    有依赖的按 depends_on 链顺序串行。
    """
    # 简化：MVP 阶段大多数场景无 depends_on，全部并行
    # 有 depends_on 的按声明顺序串行执行
    no_dep = [c for c in skill_calls if not c.depends_on]
    has_dep = [c for c in skill_calls if c.depends_on]
    if not has_dep:
        return [no_dep] if no_dep else []
    # 有依赖时简化为：先并行执行无依赖，再串行执行有依赖
    groups = [no_dep] if no_dep else []
    for call in has_dep:
        groups.append([call])
    return groups


async def skill_executor_node(state: QuestionState) -> dict[str, Any]:
    """skill_executor 节点入口。"""
    skill_calls: list[SkillCall] = state.get("skill_calls", [])
    goal = state.get("goal")

    if not skill_calls:
        logger.warning("skill_executor.empty_calls")
        return {"evidences": []}

    groups = _topo_sort(skill_calls)
    evidences: list[Evidence] = []

    for group in groups:
        if len(group) == 1:
            ev = await _execute_skill_safe(group[0], goal)
            evidences.append(ev)
        else:
            # 并行执行
            tasks = [_execute_skill_safe(c, goal) for c in group]
            group_evs = await asyncio.gather(*tasks, return_exceptions=False)
            evidences.extend(group_evs)

    logger.info(
        "skill_executor.done",
        total=len(skill_calls),
        evidences=len(evidences),
        degraded=sum(1 for ev in evidences if ev.degraded),
    )
    return {"evidences": evidences}
