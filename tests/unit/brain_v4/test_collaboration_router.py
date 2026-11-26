"""协作路由确定性测试（Task 5 Step 1）。

路由只能依赖显式特征与 profile 阈值；模型建议写入 `suggested_mode`，
不得覆盖确定性 `mode`。`alert_hot` 永不启动议会。
"""

from aistock_agent.brain_v4.contracts.deliberation import CollaborationDecision
from aistock_agent.brain_v4.deliberation.router import route_collaboration


def test_close_material_hypotheses_trigger_council():
    decision = route_collaboration(
        materiality="high",
        hypothesis_scores=[0.56, 0.51, 0.18],
        evidence_conflict_score=0.72,
        cross_layer_conflict=True,
        remaining_budget=40_000,
    )
    assert decision.mode == "ANALYST_COUNCIL"
    assert "close_competing_hypotheses" in decision.trigger_codes
    assert isinstance(decision, CollaborationDecision)


def test_alert_hot_never_starts_council():
    decision = route_collaboration(profile="alert_hot", materiality="high")
    assert decision.mode == "SOLO_DEEP"
    assert decision.selected_roles == []
    assert decision.trigger_codes == ["profile_alert_hot_no_council"]


def test_model_suggestion_cannot_override_deterministic_mode():
    decision = route_collaboration(
        materiality="low",
        hypothesis_scores=[0.55, 0.10],
        evidence_conflict_score=0.0,
        remaining_budget=40_000,
        suggested_mode="ANALYST_COUNCIL",
    )
    assert decision.mode == "SOLO_DEEP"
    assert decision.suggested_mode == "ANALYST_COUNCIL"


def test_high_materiality_publish_ready_requires_red_team():
    decision = route_collaboration(
        materiality="high",
        hypothesis_scores=[0.62, 0.40],
        evidence_conflict_score=0.1,
        remaining_budget=40_000,
        publish_ready=True,
    )
    assert decision.mode == "MANDATORY_RED_TEAM"
    assert "red_team" in decision.selected_roles


def test_professional_gap_selects_lead_with_consultants():
    decision = route_collaboration(
        materiality="medium",
        hypothesis_scores=[0.60, 0.20],
        evidence_conflict_score=0.1,
        remaining_budget=40_000,
        professional_gaps=2,
    )
    assert decision.mode == "LEAD_WITH_CONSULTANTS"
    assert decision.selected_roles


def test_council_downgrades_when_budget_too_small():
    decision = route_collaboration(
        materiality="high",
        hypothesis_scores=[0.56, 0.51],
        evidence_conflict_score=0.72,
        cross_layer_conflict=True,
        remaining_budget=1_000,
    )
    assert decision.mode == "LEAD_WITH_CONSULTANTS"
    assert "budget_insufficient_for_council" in decision.trigger_codes
