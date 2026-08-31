from aistock_agent.schemas.prediction import PredictionRisk
from aistock_agent.services.prediction_invalidation import update_trigger_state

INIT = {"state": "inactive", "below_streak": 0, "above_streak": 0}


def test_trigger_requires_two_consecutive_below_days():
    state, s1 = update_trigger_state(INIT, True)
    assert state == "inactive"          # 第 1 日跌破，未达标
    state, _ = update_trigger_state(s1, True)
    assert state == "armed"             # 第 2 日跌破 → armed


def test_single_day_flip_does_not_advance():
    state, _ = update_trigger_state(INIT, True)
    state, _ = update_trigger_state(INIT, False)  # 单日回升
    assert state == "inactive"


def test_armed_enters_de_escalating_then_releases():
    _, s1 = update_trigger_state(INIT, True)
    _, s2 = update_trigger_state(s1, True)
    assert s2["state"] == "armed"
    state, s3 = update_trigger_state(s2, False)   # 收复第 1 日
    assert state == "de_escalating"
    state, s4 = update_trigger_state(s3, False)   # 收复第 2 日
    assert state == "de_escalating"
    state, s5 = update_trigger_state(s4, False)   # 收复第 3 日 → release
    assert state == "inactive"


def test_de_escalating_returns_to_armed_on_break():
    _, s1 = update_trigger_state(INIT, True)
    _, s2 = update_trigger_state(s1, True)
    _, s3 = update_trigger_state(s2, False)
    assert s3["state"] == "de_escalating"
    state, _ = update_trigger_state(s3, True)     # 重新跌破 → 回 armed
    assert state == "armed"


def test_prediction_risk_new_fields_optional():
    r = PredictionRisk(factor="f", invalidation="i")
    assert r.indicator is None and r.triggered is False
    r2 = PredictionRisk(factor="f", invalidation="i", indicator="ma20", triggered=True)
    assert r2.indicator == "ma20" and r2.triggered is True
