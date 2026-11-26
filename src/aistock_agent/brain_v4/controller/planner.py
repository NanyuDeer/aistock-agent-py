"""确定性研究计划：把请求、竞争假设与 profile 压成可执行计划。

计划必须可复现：同一 request / hypotheses / profile / round_no 得到完全
相同的 `ResearchPlan` 与 `plan_id`。因此这里不读取系统时间，`plan_id` 由
规范化载荷摘要派生，排序规则全部显式写死。
"""

from collections.abc import Sequence
from datetime import datetime

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.research import Hypothesis, ResearchRequest
from ..graph.profiles import RunProfile
from ..graph.state import canonical_digest


class EvidenceRequirement(V4Contract):
    """单个假设需要的证据类型清单，供定向补证与工具选择。"""

    hypothesis_ref: str
    required_evidence_types: list[str] = Field(default_factory=list)


class ResearchPlan(V4Contract):
    """一轮研究的确定性计划。"""

    plan_id: str
    run_id: str
    round_no: int = Field(ge=0)
    hypothesis_refs: list[str] = Field(default_factory=list)
    evidence_requirements: list[EvidenceRequirement] = Field(default_factory=list)
    required_questions: list[str] = Field(default_factory=list)
    tool_groups: list[str] = Field(default_factory=list)
    as_of: datetime
    data_cutoff: datetime


def _dedupe_in_order(values: Sequence[str]) -> list[str]:
    """去重但保留原顺序：必答问题的顺序本身是产品语义。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def build_plan(
    request: ResearchRequest,
    hypotheses: Sequence[Hypothesis],
    profile: RunProfile,
    round_no: int = 0,
) -> ResearchPlan:
    """构建研究计划。

    假设按“置信度降序、同置信度按 id/version 升序”排序，确保高价值假设
    优先获得工具预算；排序键不含任何时间字段。
    """
    ordered_hypotheses = sorted(
        hypotheses,
        key=lambda item: (-item.prior_confidence, item.hypothesis_id, item.version),
    )
    requirements = [
        EvidenceRequirement(
            hypothesis_ref=hypothesis.ref,
            required_evidence_types=sorted(set(hypothesis.required_evidence_types)),
        )
        for hypothesis in ordered_hypotheses
    ]

    payload = {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "profile": profile.name,
        "round_no": round_no,
        "hypothesis_refs": [hypothesis.ref for hypothesis in ordered_hypotheses],
        "evidence_requirements": [
            requirement.model_dump() for requirement in requirements
        ],
        "required_questions": _dedupe_in_order(request.required_questions),
        "tool_groups": list(profile.tool_groups),
        "as_of": request.as_of.isoformat(),
        "data_cutoff": request.data_cutoff.isoformat(),
    }

    return ResearchPlan(
        plan_id=f"plan_{canonical_digest(payload)[:16]}",
        run_id=request.run_id,
        round_no=round_no,
        hypothesis_refs=[hypothesis.ref for hypothesis in ordered_hypotheses],
        evidence_requirements=requirements,
        required_questions=_dedupe_in_order(request.required_questions),
        tool_groups=list(profile.tool_groups),
        as_of=request.as_of,
        data_cutoff=request.data_cutoff,
    )
