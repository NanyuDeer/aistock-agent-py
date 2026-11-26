"""早/午/晚 profile 组合：复用不可变 `RunProfile`，不重复定义任何阈值。

约束（计划 Task 9 Step 5 / 团队接入规范 §4）：
- 时段的轮次、步数、超时、token 与工具组全部来自 `graph/profiles.py`；
- 本模块只声明输入/输出契约、parent 要求与允许的渲染出口；
- ledger namespace 直接委托 `ledger_namespace`，保证同一 run_id 在不同时段不串账本。
"""

from __future__ import annotations

from dataclasses import dataclass

from aistock_agent.brain_v4.contracts.briefing import BriefingProfile
from aistock_agent.brain_v4.graph.profiles import (
    RunProfile,
    get_profile,
    ledger_namespace,
)

RENDER_TARGETS: tuple[str, ...] = ("app", "web", "text", "audio")


@dataclass(frozen=True)
class BriefingProfileSpec:
    """一个简报时段的组合声明；资源上限委托给 `RunProfile`。"""

    name: BriefingProfile
    run_profile: RunProfile
    input_contract: str
    output_contract: str
    parent_profiles: tuple[str, ...]
    requires_parent_judgment: bool


def _spec(
    name: BriefingProfile,
    *,
    input_contract: str,
    parent_profiles: tuple[str, ...],
) -> BriefingProfileSpec:
    run_profile = get_profile(name)
    return BriefingProfileSpec(
        name=name,
        run_profile=run_profile,
        input_contract=input_contract,
        output_contract=f"{run_profile.output_contract}.v1",
        parent_profiles=parent_profiles,
        requires_parent_judgment=run_profile.requires_parent_judgment,
    )


_BRIEFING_PROFILES: dict[str, BriefingProfileSpec] = {
    "briefing_morning": _spec(
        "briefing_morning",
        input_contract="ResearchRequest.v1",
        parent_profiles=(),
    ),
    "briefing_midday": _spec(
        "briefing_midday",
        input_contract="BriefingEnvelope.v1+MorningJudgment.v1",
        parent_profiles=("briefing_morning",),
    ),
    "briefing_evening": _spec(
        "briefing_evening",
        input_contract="BriefingEnvelope.v1+MarketTraceJudgment.v1",
        parent_profiles=("briefing_morning", "briefing_midday"),
    ),
}


def get_briefing_profile(name: str) -> BriefingProfileSpec:
    """按名称取时段声明；未知名称显式报错，不静默落到默认时段。"""
    try:
        return _BRIEFING_PROFILES[name]
    except KeyError as exc:
        raise KeyError(f"unknown briefing profile: {name!r}") from exc


def briefing_ledger_namespace(profile: str, run_id: str) -> str:
    """时段独立 ledger namespace：`<profile>:<run_id>`。"""
    spec = get_briefing_profile(profile)
    return ledger_namespace(spec.name, run_id)


__all__ = [
    "BriefingProfileSpec",
    "RENDER_TARGETS",
    "briefing_ledger_namespace",
    "get_briefing_profile",
]
