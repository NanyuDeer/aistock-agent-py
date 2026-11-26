"""议事 reducer、单写提交与裁判不变量测试（Task 5 Step 4/8）。

覆盖：
- 并行立场合并与到达顺序无关；
- 分析师节点只能写 `submissions` 与 `usage`，不能写规范图；
- 议题必须绑定具体 ID，不允许开放式聊天议题；
- 异议缺 target_id/severity/kill_criterion 必须拒绝；
- 收敛门禁在无新信号时标记 no_progress；
- 唯一写者只接受 accepted 提案；
- 裁判拒绝引用冻结账本之外的证据。
"""

from datetime import datetime

import pytest

from aistock_agent.brain_v4.contracts.deliberation import (
    CaseFile,
    Objection,
    PeerBrief,
    PositionPaper,
    RevisionProposal,
)
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem, EvidenceLedger
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchGap
from aistock_agent.brain_v4.deliberation.agenda import build_agenda, merge_submissions
from aistock_agent.brain_v4.deliberation.committer import RevisionCommitter
from aistock_agent.brain_v4.deliberation.convergence import (
    InvalidObjectionError,
    evaluate_convergence,
    validate_objection,
)
from aistock_agent.brain_v4.deliberation.judge import CouncilJudge, EvidenceNotFrozenError
from aistock_agent.brain_v4.deliberation.roles import build_role_context, run_role_node

AS_OF = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
EVENT_TIME = datetime.fromisoformat("2026-11-26T09:00:00+08:00")
PUBLISHED_AT = datetime.fromisoformat("2026-11-26T09:05:00+08:00")
FETCHED_AT = datetime.fromisoformat("2026-11-26T09:10:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-26T15:00:00+08:00")


def _case() -> CaseFile:
    return CaseFile(
        case_id="case_mkt_1",
        run_id="run_1",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains the index move?",
        decision_type="market_root_cause",
        materiality="high",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_1",
        frozen_fact_ids=["fact_close"],
        hypothesis_refs=["hyp_liquidity:v1", "hyp_risk_pref:v1"],
        open_issue_ids=[],
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _hyp(hypothesis_id: str, confidence: float) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        version=1,
        statement=f"{hypothesis_id} 的解释",
        status="open",
        prior_confidence=confidence,
        disconfirmers=["出现反向证据"],
        scope="market",
    )


def _evidence(evidence_id: str, supports: list[str]) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source="unit-test",
        source_type="news",
        event_time=EVENT_TIME,
        published_at=PUBLISHED_AT,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        evidence_kind="direct",
        supports_hypothesis_refs=supports,
        data_quality="high",
        time_confidence="high",
        tool="fixture",
        response_digest=f"digest_{evidence_id}",
        snapshot_id="snap_1",
    )


def _ledger() -> EvidenceLedger:
    return EvidenceLedger(
        run_id="run_1",
        snapshot_id="snap_1",
        as_of=AS_OF,
        items=[
            _evidence("ev_liquidity", ["hyp_liquidity:v1"]),
            _evidence("ev_risk", ["hyp_risk_pref:v1"]),
        ],
    )


def _position(position_id: str, role: str, ref: str, confidence: float) -> PositionPaper:
    return PositionPaper(
        position_id=position_id,
        issue_id=f"issue_{ref.split(':', 1)[0]}",
        analyst_role=role,
        hypothesis_ref=ref,
        thesis=f"{role} 支持 {ref}",
        evidence_ids=[],
        confidence=confidence,
        disconfirmers=["出现反向证据"],
        created_at=AS_OF,
    )


def _state() -> dict:
    hypotheses = [_hyp("hyp_liquidity", 0.56), _hyp("hyp_risk_pref", 0.51)]
    return {
        "case_file": _case(),
        "evidence_ledger": _ledger(),
        "hypotheses": hypotheses,
        "round_no": 0,
        "case_version": 1,
        "agenda_items": build_agenda(_case(), hypotheses, []),
        "submissions": [],
        "peer_briefs": [],
        "belief_updates": [],
    }


def test_parallel_positions_are_order_independent():
    a = _position("pos_r00_lead", "lead_thesis", "hyp_liquidity:v1", 0.56)
    b = _position("pos_r00_alt", "alternative", "hyp_risk_pref:v1", 0.51)
    left = merge_submissions([b], [a])
    right = merge_submissions([a], [b])
    assert left == right
    assert [paper.position_id for paper in left] == ["pos_r00_alt", "pos_r00_lead"]


def test_duplicate_submission_is_deduped_deterministically():
    a = _position("pos_r00_lead", "lead_thesis", "hyp_liquidity:v1", 0.56)
    assert merge_submissions([a], [a, a]) == [a]


def test_analyst_cannot_write_canonical_graph():
    update = run_role_node("alternative", _state())
    assert "hypothesis_graph" not in update
    assert "causal_graph" not in update
    assert list(update) == ["submissions", "usage"]


def test_role_node_returns_structured_position_with_disconfirmers():
    update = run_role_node("alternative", _state())
    paper = update["submissions"][0]
    assert paper.analyst_role == "alternative"
    assert paper.hypothesis_ref == "hyp_risk_pref:v1"
    assert paper.disconfirmers
    assert paper.created_at == AS_OF


def test_round_zero_context_hides_peer_briefs():
    state = _state()
    state["peer_briefs"] = [
        PeerBrief(
            peer_brief_id="pb_r01_mechanism",
            case_id="case_mkt_1",
            case_version=1,
            deliberation_round=1,
            target_role="mechanism",
            source_artifact_ids=["pos_r00_lead"],
            competing_claims=["lead 支持 hyp_liquidity:v1"],
            generated_at=AS_OF,
        )
    ]
    context = build_role_context(round_no=0, role="mechanism", state=state)
    assert context.peer_briefs == []


def test_agenda_items_are_closed_and_id_bound():
    hypotheses = [_hyp("hyp_liquidity", 0.56), _hyp("hyp_risk_pref", 0.51)]
    items = build_agenda(_case(), hypotheses, [])
    assert [item.issue_id for item in items] == ["issue_hyp_liquidity", "issue_hyp_risk_pref"]
    assert all(item.question.startswith("Which causal explanation") for item in items)
    assert all(len(item.candidate_hypothesis_refs) == 1 for item in items)
    refs = {ref for item in items for ref in item.candidate_hypothesis_refs}
    assert refs <= {hypothesis.ref for hypothesis in hypotheses}


def test_agenda_attaches_high_materiality_gap_to_target_hypothesis():
    gap = ResearchGap(
        gap_id="gap_pre_move_trigger",
        run_id="run_1",
        issue_id="issue_hyp_liquidity",
        gap_type="time_order",
        description="time_order:pre_move_trigger",
        target_hypothesis_refs=["hyp_liquidity:v1"],
        materiality="high",
        status="open",
    )
    items = build_agenda(_case(), [_hyp("hyp_liquidity", 0.56)], [gap])
    assert "resolve_gap:gap_pre_move_trigger" in items[0].decision_criteria


def test_objection_without_kill_criterion_is_rejected():
    objection = Objection(
        objection_id="obj_bad",
        issue_id="issue_hyp_liquidity",
        raised_by="alternative",
        target_type="position",
        target_id="pos_r00_lead",
        objection_type="chronology",
        severity="high",
        argument="价格先于消息",
        kill_criterion="",
        status="open",
    )
    with pytest.raises(InvalidObjectionError):
        validate_objection(objection)


def test_convergence_marks_no_progress_without_new_signals():
    decision = evaluate_convergence(
        profile="market_trace",
        round_no=2,
        new_evidence_count=0,
        open_high_objection_ids=[],
        pending_gap_ids=[],
        ranking_changed=False,
        topology_changed=False,
        max_belief_delta=0.0,
    )
    assert decision.has_progress is False
    assert decision.next_action == "judge"
    assert decision.stop_reason == "no_progress"


def test_convergence_requests_research_for_open_gap():
    decision = evaluate_convergence(
        profile="market_trace",
        round_no=1,
        new_evidence_count=0,
        open_high_objection_ids=["obj_r01_alt"],
        pending_gap_ids=["gap_pre_move_trigger"],
        ranking_changed=False,
        topology_changed=False,
        max_belief_delta=0.10,
    )
    assert decision.has_progress is True
    assert decision.next_action == "targeted_research"


def test_single_writer_rejects_unaccepted_proposal():
    proposal = RevisionProposal(
        proposal_id="rp_1",
        issue_id="issue_hyp_liquidity",
        proposer="lead_thesis",
        target_refs=["hyp_liquidity:v1"],
        action="add_evidence",
        reason="补时间线证据",
        status="proposed",
    )
    with pytest.raises(ValueError):
        RevisionCommitter().commit(proposal, _state())


def test_single_writer_commits_accepted_proposal_once():
    proposal = RevisionProposal(
        proposal_id="rp_1",
        issue_id="issue_hyp_liquidity",
        proposer="lead_thesis",
        target_refs=["hyp_liquidity:v1"],
        action="reweight",
        reason="时间线证据削弱主因",
        triggering_evidence_ids=["ev_liquidity"],
        status="accepted",
    )
    result = RevisionCommitter().commit(proposal, _state())
    assert len(result.revision_events) == 1
    assert result.revision_events[0].action == "reweight"
    assert "hyp_liquidity:v1" in result.hypothesis_graph
    assert result.revision_events[0].created_at == AS_OF


def test_judge_rejects_evidence_outside_frozen_ledger():
    judge = CouncilJudge(frozen_evidence_ids={"ev_liquidity"}, rubric_version="rubric_v1")
    position = _position("pos_r00_lead", "lead_thesis", "hyp_liquidity:v1", 0.56)
    position = position.model_copy(update={"evidence_ids": ["ev_not_frozen"]})
    with pytest.raises(EvidenceNotFrozenError):
        judge.judge(
            case_file=_case(),
            agenda_items=build_agenda(_case(), _state()["hypotheses"], []),
            positions=[position],
            objections=[],
            belief_updates=[],
        )
