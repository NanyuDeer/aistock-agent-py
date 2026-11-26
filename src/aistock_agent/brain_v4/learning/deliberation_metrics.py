"""议会质量指标：轮次、belief 变化、少数意见保留、有效性、重复与时间泄漏。

指标只做统计，不产生新结论，也不修改任何输入。全部计算基于显式传入的
冻结记录（belief updates / minority opinions / evidence items），
时间边界由调用方以 `as_of` / `data_cutoff` 显式给出，不读系统时间。
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.deliberation import BeliefUpdate
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem

#: 需要启动议会的协作模式。
COUNCIL_MODES: frozenset[str] = frozenset({"ANALYST_COUNCIL", "MANDATORY_RED_TEAM"})

#: 这些停止原因说明议会未取得确定结论，不计为“有效”。
_INEFFECTIVE_STOPS: frozenset[str] = frozenset(
    {"insufficient", "no_progress", "escalation", "escalated"}
)


class DeliberationInputs(V4Contract):
    """一次运行的议会观测输入；全部为冻结只读记录。"""

    collaboration_mode: str
    stop_reason: str
    judgment_status: str
    belief_updates: list[BeliefUpdate] = Field(default_factory=list)
    minority_opinions_raised: list[str] = Field(default_factory=list)
    minority_opinions_retained: list[str] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    as_of: datetime
    data_cutoff: datetime


class DeliberationMetrics(V4Contract):
    """一次运行的议会质量指标快照。"""

    deliberation_rounds: int = Field(ge=0)
    belief_update_count: int = Field(ge=0)
    belief_change_count: int = Field(ge=0)
    minority_raised_count: int = Field(ge=0)
    minority_retained_count: int = Field(ge=0)
    minority_retention_rate: float = Field(ge=0.0, le=1.0)
    collaboration_mode: str
    council_mode_engaged: bool
    council_effective: bool
    duplicate_tool_call_count: int = Field(ge=0)
    time_leakage_count: int = Field(ge=0)
    judgment_status: str
    stop_reason: str


def _tool_key(item: EvidenceItem) -> str:
    return json.dumps(
        [item.tool, item.tool_params],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _count_duplicate_tools(items: list[EvidenceItem]) -> int:
    keys = [_tool_key(item) for item in items]
    return len(keys) - len(set(keys))


def _count_time_leakage(
    items: list[EvidenceItem], *, as_of: datetime, data_cutoff: datetime
) -> int:
    return sum(
        1
        for item in items
        if item.event_time > data_cutoff or item.published_at > as_of
    )


def measure_deliberation(inputs: DeliberationInputs) -> DeliberationMetrics:
    """基于冻结记录计算议会质量指标；纯函数、确定性。"""
    rounds = {update.deliberation_round for update in inputs.belief_updates}
    belief_update_count = len(inputs.belief_updates)
    belief_change_count = sum(
        1 for update in inputs.belief_updates if update.action != "hold"
    )

    raised = len(inputs.minority_opinions_raised)
    retained = len(inputs.minority_opinions_retained)
    retention_rate = 0.0 if raised == 0 else retained / raised

    council_mode_engaged = inputs.collaboration_mode in COUNCIL_MODES
    council_effective = (
        council_mode_engaged
        and belief_change_count > 0
        and inputs.stop_reason not in _INEFFECTIVE_STOPS
    )

    return DeliberationMetrics(
        deliberation_rounds=len(rounds),
        belief_update_count=belief_update_count,
        belief_change_count=belief_change_count,
        minority_raised_count=raised,
        minority_retained_count=retained,
        minority_retention_rate=retention_rate,
        collaboration_mode=inputs.collaboration_mode,
        council_mode_engaged=council_mode_engaged,
        council_effective=council_effective,
        duplicate_tool_call_count=_count_duplicate_tools(inputs.evidence_items),
        time_leakage_count=_count_time_leakage(
            inputs.evidence_items, as_of=inputs.as_of, data_cutoff=inputs.data_cutoff
        ),
        judgment_status=inputs.judgment_status,
        stop_reason=inputs.stop_reason,
    )


__all__ = [
    "COUNCIL_MODES",
    "DeliberationInputs",
    "DeliberationMetrics",
    "measure_deliberation",
]
