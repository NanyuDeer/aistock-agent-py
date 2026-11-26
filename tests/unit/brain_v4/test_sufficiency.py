"""结构化充分性门禁测试（Task 4 Step 4）。

门禁只返回 `SufficiencyReport` 与结构化 `ResearchGap`，不写叙述文本：
- 证据覆盖（含必需要证据类型）；
- 时间顺序（published_at 不得晚于 as_of，event_time 不得晚于 data_cutoff）；
- 机制、量级、替代解释、失效条件；
- 无支撑边比例。

`as_of` / `data_cutoff` 必须显式传入；同一输入必须得到逐字节一致的报告。
"""

from datetime import datetime

from aistock_agent.brain_v4.contracts.evidence import EvidenceItem
from aistock_agent.brain_v4.contracts.research import CausalEdge, Hypothesis
from aistock_agent.brain_v4.controller.sufficiency import evaluate_sufficiency

AS_OF = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-26T09:30:00+08:00")
VALID_TIME = datetime.fromisoformat("2026-11-26T09:00:00+08:00")


def _hyp(
    hypothesis_id: str,
    *,
    confidence: float = 0.6,
    required: list[str] | None = None,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        version=1,
        statement=f"{hypothesis_id} 的解释",
        status="open",
        prior_confidence=confidence,
        disconfirmers=["出现反向证据"],
        required_evidence_types=required or ["news"],
        scope="market",
    )


def _evidence(
    evidence_id: str,
    *,
    supports: list[str] | None = None,
    source_type: str = "news",
    event_time: datetime | None = None,
    published_at: datetime | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source="unit-test",
        source_type=source_type,
        event_time=event_time or datetime.fromisoformat("2026-11-26T08:30:00+08:00"),
        published_at=published_at or datetime.fromisoformat("2026-11-26T08:35:00+08:00"),
        fetched_at=datetime.fromisoformat("2026-11-26T08:40:00+08:00"),
        as_of=AS_OF,
        evidence_kind="direct",
        supports_hypothesis_refs=supports or [],
        contradicts_hypothesis_refs=[],
        data_quality="high",
        time_confidence="high",
        tool="unit-test",
        response_digest=f"digest-{evidence_id}",
        snapshot_id="snap-1",
    )


def _edge(
    edge_id: str,
    from_node: str,
    to_node: str,
    *,
    mechanism: str = "机制清晰",
    magnitude_check: str = "量级匹配",
    counterfactual_check: str = "反事实可检验",
    supporting: list[str] | None = None,
    edge_type: str = "inference",
) -> CausalEdge:
    return CausalEdge(
        edge_id=edge_id,
        from_node=from_node,
        to_node=to_node,
        mechanism=mechanism,
        supporting_evidence_ids=supporting if supporting is not None else ["ev_a"],
        contradicting_evidence_ids=[],
        edge_type=edge_type,
        confidence=0.6,
        valid_time=VALID_TIME,
        magnitude_check=magnitude_check,
        counterfactual_check=counterfactual_check,
    )


def _passing_inputs() -> dict[str, object]:
    hypotheses = [_hyp("hyp_a"), _hyp("hyp_b")]
    evidence = [
        _evidence("ev_a", supports=["hyp_a:v1"]),
        _evidence("ev_b", supports=["hyp_b:v1"]),
    ]
    edges = [
        _edge("e1", "hyp_a:v1", "claim_x", supporting=["ev_a"]),
        _edge("e2", "hyp_b:v1", "claim_y", supporting=["ev_b"]),
    ]
    return {
        "run_id": "run-1",
        "issue_id": "issue-1",
        "as_of": AS_OF,
        "data_cutoff": DATA_CUTOFF,
        "hypotheses": hypotheses,
        "evidence": evidence,
        "causal_edges": edges,
        "required_evidence_types": {
            "hyp_a:v1": ["news"],
            "hyp_b:v1": ["news"],
        },
    }


def _run(**overrides: object):
    payload = _passing_inputs()
    payload.update(overrides)
    return evaluate_sufficiency(**payload)


def _gap_types(report: object) -> set[str]:
    return {gap.gap_type for gap in report.gaps}  # type: ignore[attr-defined]


def test_gate_passes_with_full_coverage_and_supported_edges():
    report = _run()
    assert report.passed is True
    assert report.gaps == []
    assert report.unsupported_edge_ratio == 0.0


def test_missing_supporting_evidence_creates_missing_evidence_gap():
    report = _run(evidence=[_evidence("ev_a", supports=["hyp_a:v1"])])
    assert report.passed is False
    assert "missing_evidence" in _gap_types(report)
    assert any("hyp_b:v1" in gap.target_hypothesis_refs for gap in report.gaps)


def test_required_evidence_type_not_covered_creates_missing_evidence_gap():
    report = _run(required_evidence_types={"hyp_a:v1": ["filing"], "hyp_b:v1": ["news"]})
    assert report.passed is False
    missing = [gap for gap in report.gaps if gap.gap_type == "missing_evidence"]
    assert any("filing" in gap.required_evidence_types for gap in missing)


def test_evidence_published_after_as_of_creates_time_order_gap():
    late = _evidence(
        "ev_late",
        supports=["hyp_b:v1"],
        published_at=datetime.fromisoformat("2026-11-26T10:30:00+08:00"),
    )
    report = _run(evidence=[_evidence("ev_a", supports=["hyp_a:v1"]), late])
    assert report.passed is False
    assert "time_order" in _gap_types(report)


def test_edge_without_mechanism_creates_mechanism_gap():
    edges = [
        _edge("e1", "hyp_a:v1", "claim_x", supporting=["ev_a"], mechanism=""),
        _edge("e2", "hyp_b:v1", "claim_y", supporting=["ev_b"]),
    ]
    report = _run(causal_edges=edges)
    assert report.passed is False
    assert "mechanism" in _gap_types(report)


def test_edge_without_magnitude_check_creates_magnitude_gap():
    edges = [
        _edge("e1", "hyp_a:v1", "claim_x", supporting=["ev_a"], magnitude_check=""),
        _edge("e2", "hyp_b:v1", "claim_y", supporting=["ev_b"]),
    ]
    report = _run(causal_edges=edges)
    assert report.passed is False
    assert "magnitude" in _gap_types(report)


def test_edge_without_counterfactual_check_creates_invalidator_gap():
    edges = [
        _edge("e1", "hyp_a:v1", "claim_x", supporting=["ev_a"], counterfactual_check=""),
        _edge("e2", "hyp_b:v1", "claim_y", supporting=["ev_b"]),
    ]
    report = _run(causal_edges=edges)
    assert report.passed is False
    assert "invalidator" in _gap_types(report)


def test_single_hypothesis_creates_alternative_gap():
    report = _run(
        hypotheses=[_hyp("hyp_a")],
        evidence=[_evidence("ev_a", supports=["hyp_a:v1"])],
        causal_edges=[_edge("e1", "hyp_a:v1", "claim_x", supporting=["ev_a"])],
        required_evidence_types={"hyp_a:v1": ["news"]},
    )
    assert report.passed is False
    assert "alternative" in _gap_types(report)


def test_unsupported_edges_raise_ratio_and_edge_support_gap():
    edges = [
        _edge("e1", "hyp_a:v1", "claim_x", supporting=[]),
        _edge("e2", "hyp_b:v1", "claim_y", supporting=[], edge_type="hypothesis"),
    ]
    report = _run(causal_edges=edges)
    assert report.unsupported_edge_ratio == 1.0
    assert "edge_support" in _gap_types(report)
    assert report.passed is False


def test_report_is_deterministic_and_echoes_explicit_time_boundaries():
    first = _run()
    second = _run()
    assert first.model_dump() == second.model_dump()
    assert first.as_of == AS_OF
    assert first.data_cutoff == DATA_CUTOFF
