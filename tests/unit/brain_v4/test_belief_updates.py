"""PeerBrief / BeliefUpdate / InfluenceEdge 与反羊群测试（Task 5 Step 5）。

核心不变量：
- 同伴观点不是证据，不能只凭同伴观点提高置信度；
- 读取 PeerBrief 后即使选择 hold 也必须落一条 BeliefUpdate；
- 同伴摘要可以改变检索方向并请求 Gap；
- PeerBrief 不得进入 EvidenceLedger。
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from aistock_agent.brain_v4.contracts.deliberation import CaseFile, PeerBrief
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.research import Hypothesis
from aistock_agent.brain_v4.deliberation.beliefs import (
    belief_update_from_peer_brief,
    make_hold_update,
    validate_belief_update,
)
from aistock_agent.brain_v4.deliberation.roles import build_role_context

AS_OF = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
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
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _state_with_peer_brief() -> dict:
    hypothesis = Hypothesis(
        hypothesis_id="hyp_liquidity",
        version=1,
        statement="流动性是主因",
        status="open",
        prior_confidence=0.56,
        disconfirmers=["出现反向证据"],
        scope="market",
    )
    brief = PeerBrief(
        peer_brief_id="pb_r01_mechanism",
        case_id="case_mkt_1",
        case_version=1,
        deliberation_round=1,
        target_role="mechanism",
        source_artifact_ids=["pos_r00_lead"],
        competing_claims=["lead 支持 hyp_liquidity:v1"],
        generated_at=AS_OF,
    )
    return {
        "case_file": _case(),
        "hypotheses": [hypothesis],
        "round_no": 0,
        "case_version": 1,
        "peer_briefs": [brief],
        "belief_updates": [],
    }


def peer_brief_with_timeline_conflict() -> PeerBrief:
    return PeerBrief(
        peer_brief_id="pb_r01_lead",
        case_id="case_mkt_1",
        case_version=1,
        deliberation_round=1,
        target_role="lead_thesis",
        source_artifact_ids=["pos_r00_timeline"],
        competing_claims=["timeline: 价格先于消息"],
        evidence_ids=["ev_price"],
        material_disagreements=["timeline_conflict"],
        open_questions=["gap_pre_move_trigger"],
        omitted_sections=["raw_reasoning", "private_scratch"],
        generated_at=AS_OF,
    )


def test_round_zero_positions_cannot_see_peers():
    context = build_role_context(round_no=0, role="mechanism", state=_state_with_peer_brief())
    assert context.peer_briefs == []


def test_round_one_positions_can_see_their_peer_brief():
    context = build_role_context(round_no=1, role="mechanism", state=_state_with_peer_brief())
    assert [brief.peer_brief_id for brief in context.peer_briefs] == ["pb_r01_mechanism"]


def test_peer_opinion_without_evidence_cannot_raise_confidence():
    update = validate_belief_update(
        prior_confidence=0.55,
        posterior_confidence=0.70,
        influencing_artifact_ids=["pos_alt_1"],
        evidence_ids=[],
    )
    assert update.accepted is False
    assert update.reason == "confidence_increase_requires_evidence_or_mechanism_change"


def test_peer_opinion_with_evidence_may_raise_confidence():
    update = validate_belief_update(
        prior_confidence=0.55,
        posterior_confidence=0.70,
        influencing_artifact_ids=["pos_alt_1"],
        evidence_ids=["ev_new"],
    )
    assert update.accepted is True
    assert update.reason is None


def test_mechanism_change_allows_confidence_increase_without_new_evidence():
    update = validate_belief_update(
        prior_confidence=0.50,
        posterior_confidence=0.60,
        influencing_artifact_ids=["pos_alt_1"],
        evidence_ids=[],
        mechanism_changed=True,
    )
    assert update.accepted is True


def test_peer_brief_can_redirect_research():
    update = belief_update_from_peer_brief(
        peer_brief_with_timeline_conflict(),
        prior_hypothesis_ref="hyp_liquidity:v1",
        prior_confidence=0.56,
        role="lead_thesis",
        round_no=1,
        case_version=1,
    )
    assert update.action == "weaken"
    assert update.requested_gap_ids == ["gap_pre_move_trigger"]
    assert update.influence_edges[0].influence_type == "search_direction"
    assert update.influence_edges[0].evidence_backed is False
    assert update.posterior_confidence[0] < update.prior_confidence


def test_hold_is_still_a_belief_update():
    update = make_hold_update(
        role="timeline",
        issue_id="issue_hyp_liquidity",
        case_version=1,
        round_no=1,
        prior_hypothesis_ref="hyp_liquidity:v1",
        prior_confidence=0.56,
    )
    assert update.action == "hold"
    assert update.posterior_hypothesis_refs == ["hyp_liquidity:v1"]
    assert update.posterior_confidence == [0.56]
    assert update.influence_edges == []


def test_peer_brief_cannot_become_evidence_ledger_item():
    with pytest.raises(ValidationError):
        EvidenceLedger(
            run_id="run_1",
            snapshot_id="snap_1",
            as_of=AS_OF,
            items=[peer_brief_with_timeline_conflict().model_dump()],
        )
