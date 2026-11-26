"""停止策略测试：严格覆盖 sufficient > stall > unstable > budget > continue 五个分支。

判定顺序是硬约束：上一轮若已充分，即使同时命中 stall/预算也必须记为 sufficient，
否则回放时 stop_reason 会随无关字段漂移。
"""

from aistock_agent.brain_v4.contracts.research import RoundLedger
from aistock_agent.brain_v4.controller.stop_policy import BudgetState, evaluate_stop


def _ledger(**overrides: object) -> RoundLedger:
    payload: dict[str, object] = {
        "round_no": 0,
        "next_action": "continue",
        "progress_score": 0.5,
        "stall_count": 0,
        "revision_budget_left": 1,
    }
    payload.update(overrides)
    return RoundLedger.model_validate(payload)


def _budget(**overrides: object) -> BudgetState:
    payload: dict[str, object] = {
        "tokens_remaining": 10,
        "steps_remaining": 10,
        "wall_time_remaining_s": 10.0,
    }
    payload.update(overrides)
    return BudgetState.model_validate(payload)


def test_sufficient_takes_priority_over_every_other_signal():
    decision = evaluate_stop(
        _ledger(sufficiency_passed=True, stall_count=5, oscillation_detected=True),
        _budget(tokens_remaining=0),
    )
    assert decision.stop is True
    assert decision.reason == "sufficient"


def test_stall_takes_priority_over_unstable_and_budget():
    decision = evaluate_stop(
        _ledger(stall_count=2, oscillation_detected=True),
        _budget(tokens_remaining=0),
    )
    assert decision.stop is True
    assert decision.reason == "stall"


def test_unstable_takes_priority_over_budget():
    decision = evaluate_stop(
        _ledger(stall_count=1, oscillation_detected=True),
        _budget(tokens_remaining=0),
    )
    assert decision.stop is True
    assert decision.reason == "unstable"


def test_budget_exhaustion_stops_when_quality_signals_are_clean():
    decision = evaluate_stop(_ledger(), _budget(steps_remaining=0))
    assert decision.stop is True
    assert decision.reason == "budget"


def test_clean_ledger_and_healthy_budget_continue():
    decision = evaluate_stop(_ledger(), _budget())
    assert decision.stop is False
    assert decision.reason == "continue"
