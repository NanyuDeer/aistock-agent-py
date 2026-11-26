"""确定性研究计划测试（Task 4 授权新增）。

`build_plan` 只依赖 request / hypotheses / profile 与显式 `round_no`：
- 同输入同输出，plan_id 可复现；
- 假设按置信度降序、同置信度按 id 升序排序；
- 证据需求与必答问题结构化输出，工具组只取 profile 声明值。
"""

from datetime import datetime

from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest
from aistock_agent.brain_v4.controller.planner import build_plan
from aistock_agent.brain_v4.graph.profiles import get_profile

AS_OF = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-26T09:30:00+08:00")


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req-1",
        run_id="run-1",
        profile="market_trace",
        subject_type="market",
        subject_code="000001-SH",
        research_goal="解释今日下跌",
        required_questions=["隔夜发生了什么", "今日主要变量是什么", "隔夜发生了什么"],
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=DATA_CUTOFF,
        snapshot_id="snap-1",
        config_version="cfg-1",
        model_version="model-1",
        prompt_version="prompt-1",
        method_version="method-1",
        budget_policy="default",
        stop_policy="default",
    )


def _hyp(
    hypothesis_id: str,
    *,
    confidence: float,
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


def test_hypotheses_are_ordered_by_confidence_desc_then_id():
    plan = build_plan(
        _request(),
        [
            _hyp("hyp_low", confidence=0.3),
            _hyp("hyp_high_b", confidence=0.8),
            _hyp("hyp_high_a", confidence=0.8),
        ],
        get_profile("market_trace"),
    )
    assert plan.hypothesis_refs == ["hyp_high_a:v1", "hyp_high_b:v1", "hyp_low:v1"]


def test_plan_collects_structured_evidence_requirements():
    plan = build_plan(
        _request(),
        [
            _hyp("hyp_a", confidence=0.7, required=["news", "filing"]),
            _hyp("hyp_b", confidence=0.4, required=["trend"]),
        ],
        get_profile("market_trace"),
    )
    by_ref = {req.hypothesis_ref: req.required_evidence_types for req in plan.evidence_requirements}
    assert by_ref["hyp_a:v1"] == ["filing", "news"]
    assert by_ref["hyp_b:v1"] == ["trend"]


def test_plan_deduplicates_request_questions_and_keeps_order():
    plan = build_plan(_request(), [_hyp("hyp_a", confidence=0.5)], get_profile("market_trace"))
    assert plan.required_questions == ["隔夜发生了什么", "今日主要变量是什么"]


def test_plan_uses_profile_tool_groups():
    profile = get_profile("market_trace")
    plan = build_plan(_request(), [_hyp("hyp_a", confidence=0.5)], profile)
    assert plan.tool_groups == profile.tool_groups


def test_plan_is_deterministic_for_same_inputs():
    first = build_plan(_request(), [_hyp("hyp_a", confidence=0.5)], get_profile("market_trace"))
    second = build_plan(_request(), [_hyp("hyp_a", confidence=0.5)], get_profile("market_trace"))
    assert first.model_dump() == second.model_dump()
    assert first.plan_id == second.plan_id


def test_plan_id_changes_when_hypothesis_set_changes():
    first = build_plan(_request(), [_hyp("hyp_a", confidence=0.5)], get_profile("market_trace"))
    second = build_plan(
        _request(),
        [_hyp("hyp_a", confidence=0.5), _hyp("hyp_b", confidence=0.4)],
        get_profile("market_trace"),
    )
    assert first.plan_id != second.plan_id


def test_round_no_is_explicit_and_defaults_to_zero():
    default_plan = build_plan(
        _request(), [_hyp("hyp_a", confidence=0.5)], get_profile("market_trace")
    )
    explicit_plan = build_plan(
        _request(),
        [_hyp("hyp_a", confidence=0.5)],
        get_profile("market_trace"),
        round_no=2,
    )
    assert default_plan.round_no == 0
    assert explicit_plan.round_no == 2
