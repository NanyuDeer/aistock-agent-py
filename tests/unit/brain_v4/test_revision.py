"""研究回改与影响分析测试（Task 4 Step 1-3）。

覆盖计划原文的“流动性主因被防御/成长背离降级为放大器”回归，以及：
- 影响遍历只触达下游 claim/hypothesis，不波及兄弟假设与无关证据；
- A→B→A 振荡显式标记 unstable；
- 同一假设第 3 次回改被回改预算策略拒绝。

fixture 的因果图决定受影响 claim，实现不得靠字符串匹配猜结论。
"""

from datetime import datetime

from aistock_agent.brain_v4.contracts.research import CausalEdge, Hypothesis
from aistock_agent.brain_v4.controller.impact import impacted_refs
from aistock_agent.brain_v4.controller.revision import Contradiction, revise

VALID_TIME = datetime.fromisoformat("2026-11-26T09:30:00+08:00")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def liquidity_root_v1() -> Hypothesis:
    """待回改的根因假设：流动性是主因。"""
    return Hypothesis(
        hypothesis_id="hyp_liquidity",
        version=1,
        statement="流动性是主因",
        status="supported",
        prior_confidence=0.7,
        assumptions=["北向资金收紧"],
        disconfirmers=["防御与成长同步上涨"],
        required_evidence_types=["liquidity_flow"],
        scope="market",
    )


def defensive_up_growth_down() -> Contradiction:
    """矛盾：防御上涨、成长下跌，说明流动性只能解释放大器而非根因。"""
    return Contradiction(
        contradiction_id="contra_defensive_up",
        target_ref="hyp_liquidity:v1",
        observed_pattern="防御板块上涨而成长板块下跌",
        suggested_statement="流动性是放大器",
        suggested_assumptions=["风格分化由风险偏好驱动"],
        suggested_disconfirmers=["防御与成长同步下跌"],
        triggering_evidence_ids=["ev_style_divergence"],
        reason="风格背离不支持流动性单因子根因",
    )


def _edge(
    edge_id: str,
    from_node: str,
    to_node: str,
    *,
    edge_type: str = "inference",
    supporting: list[str] | None = None,
) -> CausalEdge:
    return CausalEdge(
        edge_id=edge_id,
        from_node=from_node,
        to_node=to_node,
        mechanism="流动性收紧压制估值",
        supporting_evidence_ids=supporting or ["ev_liquidity"],
        contradicting_evidence_ids=[],
        edge_type=edge_type,
        confidence=0.6,
        valid_time=VALID_TIME,
        magnitude_check="流动性收缩1个标准差对应估值-2%",
        counterfactual_check="若流动性未收紧则不成因",
    )


def market_graph() -> list[CausalEdge]:
    """根因下游只有两个 claim；兄弟假设挂在无关分支上。"""
    return [
        _edge("edge_root_to_market", "hyp_liquidity:v1", "claim_market_root"),
        _edge("edge_market_to_repricing", "claim_market_root", "claim_repricing"),
        # 无关分支：从政策 claim 指向兄弟假设，不应被流动性回改波及。
        _edge("edge_policy_to_sentiment", "claim_policy", "hyp_sentiment:v1"),
        _edge("edge_sentiment_to_claim", "hyp_sentiment:v1", "claim_sentiment"),
    ]


def _contradiction(target_ref: str, statement: str, cid: str) -> Contradiction:
    return Contradiction(
        contradiction_id=cid,
        target_ref=target_ref,
        observed_pattern="风格或资金面出现反向证据",
        suggested_statement=statement,
        suggested_assumptions=["新证据改变解释层级"],
        suggested_disconfirmers=["新证据被证伪"],
        triggering_evidence_ids=[f"ev_{cid}"],
        reason="矛盾证据要求局部回改",
    )


def test_liquidity_root_is_revised_to_amplifier_when_style_diverges():
    decision = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[defensive_up_growth_down()],
        graph=market_graph(),
    )
    assert decision.action == "revise_hypothesis"
    assert decision.new_hypothesis is not None
    assert decision.new_hypothesis.statement == "流动性是放大器"
    assert decision.impacted_claim_ids == ["claim_market_root", "claim_repricing"]


def test_revised_hypothesis_is_a_new_version_with_parent_link():
    decision = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[defensive_up_growth_down()],
        graph=market_graph(),
    )
    assert decision.revision_no == 1
    assert decision.target_ref == "hyp_liquidity:v1"
    assert decision.new_hypothesis is not None
    assert decision.new_hypothesis.ref == "hyp_liquidity:v2"
    assert decision.new_hypothesis.parent_ids == ["hyp_liquidity:v1"]


def test_impact_traversal_does_not_touch_sibling_hypothesis_or_claims():
    result = impacted_refs(market_graph(), "hyp_liquidity:v1")
    assert result.impacted_claim_ids == ["claim_market_root", "claim_repricing"]
    assert "hyp_sentiment:v1" not in result.impacted_hypothesis_refs
    assert "claim_sentiment" not in result.impacted_claim_ids


def test_impact_traversal_is_downstream_only():
    result = impacted_refs(market_graph(), "claim_repricing")
    assert result.impacted_claim_ids == []
    assert result.impacted_hypothesis_refs == []


def test_revision_is_deterministic_for_identical_inputs():
    first = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[defensive_up_growth_down()],
        graph=market_graph(),
    )
    second = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[defensive_up_growth_down()],
        graph=market_graph(),
    )
    assert first.model_dump() == second.model_dump()


def test_a_to_b_to_a_oscillation_is_marked_unstable():
    first = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[_contradiction("hyp_liquidity:v1", "流动性是放大器", "contra_b")],
        graph=market_graph(),
    )
    assert first.new_hypothesis is not None

    second = revise(
        hypothesis=first.new_hypothesis,
        contradictions=[
            _contradiction(first.new_hypothesis.ref, "流动性是主因", "contra_a_again")
        ],
        graph=market_graph(),
        history=[first],
    )
    assert second.action == "revise_hypothesis"
    assert second.oscillation is True
    assert second.revision_no == 2


def test_third_revision_of_same_hypothesis_is_rejected_by_budget_policy():
    first = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[_contradiction("hyp_liquidity:v1", "流动性是放大器", "contra_b")],
        graph=market_graph(),
    )
    assert first.new_hypothesis is not None

    second = revise(
        hypothesis=first.new_hypothesis,
        contradictions=[
            _contradiction(first.new_hypothesis.ref, "流动性是主因", "contra_a_again")
        ],
        graph=market_graph(),
        history=[first],
    )
    assert second.new_hypothesis is not None

    third = revise(
        hypothesis=second.new_hypothesis,
        contradictions=[
            _contradiction(second.new_hypothesis.ref, "流动性是放大器", "contra_b_again")
        ],
        graph=market_graph(),
        history=[first, second],
    )
    assert third.action == "reject"
    assert third.new_hypothesis is None
    assert "回改" in third.reason
    assert "2" in third.reason
