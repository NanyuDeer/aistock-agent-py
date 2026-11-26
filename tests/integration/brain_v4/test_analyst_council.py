"""完整的案卷制分析师议会多轮集成测试（Task 5 Step 9）。

合成一个“流动性与风险偏好假设接近”的市场案卷，离线确定性运行：
R0 独立立论 → PeerBrief 揭示 → 交叉质询 → BeliefUpdate → 定向研究
→ 只重跑受影响席位 → R2 排名稳定 → 单写提交 → 裁判保留少数意见。

不调用 LLM、不读系统时间。
"""

from datetime import datetime

from aistock_agent.brain_v4.contracts.deliberation import CaseFile
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem, EvidenceLedger
from aistock_agent.brain_v4.contracts.research import CausalEdge, Hypothesis
from aistock_agent.brain_v4.deliberation.graph import build_council_graph
from aistock_agent.brain_v4.deliberation.router import route_collaboration

AS_OF = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
EVENT_TIME = datetime.fromisoformat("2026-11-26T09:00:00+08:00")
PUBLISHED_AT = datetime.fromisoformat("2026-11-26T09:05:00+08:00")
FETCHED_AT = datetime.fromisoformat("2026-11-26T09:10:00+08:00")
VALID_TIME = datetime.fromisoformat("2026-11-26T09:00:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-26T15:00:00+08:00")


def _case() -> CaseFile:
    return CaseFile(
        case_id="case_council_1",
        run_id="run_council_1",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains today's index move?",
        decision_type="market_root_cause",
        materiality="high",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_council",
        frozen_fact_ids=["fact_index_close"],
        hypothesis_refs=["hyp_liquidity:v1", "hyp_risk_pref:v1"],
        open_issue_ids=["issue_hyp_liquidity", "issue_hyp_risk_pref"],
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _hypotheses() -> list[Hypothesis]:
    return [
        Hypothesis(
            hypothesis_id="hyp_liquidity",
            version=1,
            statement="流动性收缩是主因",
            status="open",
            prior_confidence=0.56,
            assumptions=["北向资金净流出"],
            disconfirmers=["北向资金转为净流入"],
            scope="market",
        ),
        Hypothesis(
            hypothesis_id="hyp_risk_pref",
            version=1,
            statement="风险偏好回落是主因",
            status="open",
            prior_confidence=0.51,
            assumptions=["高 beta 板块领跌"],
            disconfirmers=["高 beta 板块逆势上涨"],
            scope="market",
        ),
    ]


def _evidence(evidence_id: str, supports: list[str], source_type: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source="integration-fixture",
        source_type=source_type,
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
        snapshot_id="snap_council",
    )


def _ledger() -> EvidenceLedger:
    return EvidenceLedger(
        run_id="run_council_1",
        snapshot_id="snap_council",
        as_of=AS_OF,
        items=[
            _evidence("ev_liquidity", ["hyp_liquidity:v1"], "fund_flow"),
            _evidence("ev_risk", ["hyp_risk_pref:v1"], "style"),
        ],
    )


def _edges() -> list[CausalEdge]:
    return [
        CausalEdge(
            edge_id="edge_liq_index",
            from_node="hyp_liquidity",
            to_node="index_move",
            mechanism="流动性收缩压缩估值",
            supporting_evidence_ids=["ev_liquidity"],
            edge_type="inference",
            confidence=0.55,
            valid_time=VALID_TIME,
            magnitude_check="指数下跌 1.2%",
            counterfactual_check="若北向净流入则指数不应下跌",
        ),
        CausalEdge(
            edge_id="edge_risk_index",
            from_node="hyp_risk_pref",
            to_node="index_move",
            mechanism="风险偏好回落压制高 beta",
            supporting_evidence_ids=["ev_risk"],
            edge_type="inference",
            confidence=0.5,
            valid_time=VALID_TIME,
            magnitude_check="高 beta 板块下跌 2.1%",
            counterfactual_check="若高 beta 领涨则风险偏好未回落",
        ),
    ]


def _initial_state() -> dict:
    collaboration = route_collaboration(
        profile="market_trace",
        materiality="high",
        hypothesis_scores=[0.56, 0.51, 0.18],
        evidence_conflict_score=0.72,
        cross_layer_conflict=True,
        remaining_budget=40_000,
    )
    return {
        "case_file": _case(),
        "evidence_ledger": _ledger(),
        "hypotheses": _hypotheses(),
        "causal_graph": _edges(),
        "collaboration_decision": collaboration,
        "research_gaps": [],
        "conflicts": [],
    }


def test_analyst_council_full_rounds():
    graph = build_council_graph()
    final = graph.invoke(_initial_state(), config={"recursion_limit": 100})

    positions = final["submissions"]
    rounds_with_positions = sorted({paper.position_id[5:7] for paper in positions})
    # R0 独立立论先发生，R1 只交换摘要、不新增立场。
    assert "00" in rounds_with_positions
    assert "01" not in rounds_with_positions
    assert min(brief.deliberation_round for brief in final["peer_briefs"]) == 1
    assert all(brief.deliberation_round >= 1 for brief in final["peer_briefs"])

    # R1 PeerBrief 使一名分析师 weaken 并请求 time gap。
    weak_updates = [
        update
        for update in final["belief_updates"]
        if update.deliberation_round == 1 and update.action == "weaken"
    ]
    assert len(weak_updates) == 1
    assert weak_updates[0].requested_gap_ids == ["gap_pre_move_trigger"]
    assert any(
        edge.influence_type == "search_direction" for edge in final["influence_edges"]
    )

    # 定向研究递增 case_version 并落一条研究证据。
    assert final["case_version"] == 2
    evidence_ids = {item.evidence_id for item in final["evidence_ledger"].items}
    assert "ev_research_v2" in evidence_ids

    # 只重跑受影响席位（R2 仅 lead_thesis 产生新立场）。
    round_two_positions = [
        paper for paper in positions if paper.position_id.startswith("pos_r02")
    ]
    assert {paper.analyst_role for paper in round_two_positions} == {"lead_thesis"}
    assert set(final["affected_role_ids"]) == {"lead_thesis"}

    # R2 排名稳定。
    ledgers = {ledger.deliberation_round: ledger for ledger in final["deliberation_round_ledgers"]}
    assert ledgers[2].ranking_changed is False

    # 唯一写者提交一次，且没有并发更新冲突。
    assert len(final["revision_events"]) == 1
    assert final["revision_events"][0].action == "add_evidence"
    assert final["conflicts"] == []

    # 裁判保留少数意见。
    decision = final["council_decision"]
    assert decision.minority_opinions
    assert decision.selected_hypothesis_refs
