"""议程构建与并行立场归并。

- 每个议题只围绕一个带 ID 的假设（claim）展开，并声明候选假设引用与
  裁决标准；不生成“讨论一下市场”这类开放议题。
- `merge_submissions` 是 append-only reducer：任意到达顺序得到同一结果，
  按 `position_id` 稳定排序；同 ID 不同内容时保留规范化摘要较小者。
"""

from collections.abc import Sequence

from ..contracts.deliberation import AgendaItem, CaseFile, PositionPaper
from ..contracts.research import Hypothesis, ResearchGap
from ..graph.state import merge_by_id
from .roles import LEAD_ROLE

_BASE_CRITERIA: tuple[str, ...] = (
    "alternative_explanations",
    "evidence_quality",
    "magnitude",
    "mechanism",
    "time_order",
)

_OWNER_BY_INDEX: tuple[str, ...] = (LEAD_ROLE, "alternative", "mechanism")


def _owner_role(index: int) -> str:
    return _OWNER_BY_INDEX[index] if index < len(_OWNER_BY_INDEX) else "mechanism"


def build_agenda(
    case_file: CaseFile,
    hypotheses: Sequence[Hypothesis],
    gaps: Sequence[ResearchGap] = (),
) -> list[AgendaItem]:
    """把假设与高材料性缺口转成可裁决议题。"""
    gaps_by_ref: dict[str, list[str]] = {}
    for gap in sorted(gaps, key=lambda entry: entry.gap_id):
        if gap.materiality != "high":
            continue
        for ref in gap.target_hypothesis_refs:
            gaps_by_ref.setdefault(ref, []).append(f"resolve_gap:{gap.gap_id}")

    items: list[AgendaItem] = []
    for index, hypothesis in enumerate(
        sorted(hypotheses, key=lambda item: item.ref)
    ):
        issue_id = f"issue_{hypothesis.hypothesis_id}"
        criteria = [*_BASE_CRITERIA, *gaps_by_ref.get(hypothesis.ref, [])]
        items.append(
            AgendaItem(
                issue_id=issue_id,
                case_id=case_file.case_id,
                question=(
                    "Which causal explanation should be preferred: "
                    f"{hypothesis.ref}?"
                ),
                candidate_hypothesis_refs=[hypothesis.ref],
                decision_criteria=sorted(criteria),
                materiality=case_file.materiality,
                owner_role=_owner_role(index),
                status="open",
                opened_at=case_file.as_of,
            )
        )
    return sorted(items, key=lambda item: item.issue_id)


def merge_submissions(*groups: Sequence[PositionPaper]) -> list[PositionPaper]:
    """按 `position_id` 归并任意数量的提交批次，结果与到达顺序无关。"""
    flat = [paper.model_dump() for group in groups for paper in group]
    merged = merge_by_id([], flat, "position_id")
    return [PositionPaper.model_validate(item) for item in merged.items]


def merge_submission_batches(
    left: Sequence[PositionPaper], right: Sequence[PositionPaper]
) -> list[PositionPaper]:
    """LangGraph reducer 入口：固定两参数，语义与 `merge_submissions` 一致。"""
    return merge_submissions(left, right)
