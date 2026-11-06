"""存量文案不回归：生产上真实出现过的工程语必须被守住，且新文案全部过门禁。"""
import pytest

from aistock_agent.services.user_facing_copy import check_copy, find_engineering_terms

# 2026-11-06 生产卡实读的 effect 文案（改前），改后必须全部被守卫拦下
CURRENT_EFFECT_TEXTS = [
    "base→0，禁止 +1",
    "base=2（趋势不可判），禁止 +1",
    "base 强制 0",
    "仅参与 certainty，不改档",
    "不改档（闸门命中，禁止 +1）",
]

EXPECTED_EFFECT_TEXTS = [
    "一票否决：直接压到空仓",
    "趋势判不了，先按两成算，但不能加",
    "直接压到空仓",
    "只影响把握程度，不改仓位",
    "暂不加仓（关键条件不满足）",
]


def test_current_copy_is_flagged():
    """证明守卫真能拦住存量问题（不是摆设）。"""
    for text in CURRENT_EFFECT_TEXTS:
        with pytest.raises(ValueError):
            check_copy(text)


def test_new_copy_passes():
    for text in EXPECTED_EFFECT_TEXTS:
        check_copy(text)


def _ledger(**kwargs):
    """用真实 builder 产出账本，覆盖各分支。"""
    from aistock_agent.services.rhythm_engine import _build_position_ledger

    defaults = dict(
        state="none", strength=None, mainline=None, bars=25,
        close=3689.91, ma20=3701.53, bull=False,
        gate_hit=True, gate_blocked=True, trend_known=True, ml_breakdown=False,
        base=0, adj=0, final=0, event_d=None, event_result=None,
        event_off_index=0, event_applied_kind=None, breadth=None,
    )
    defaults.update(kwargs)
    return _build_position_ledger(**defaults)


LEDGER_CASES = {
    "闸门命中": dict(),
    "趋势不可判": dict(gate_hit=False, gate_blocked=True, trend_known=False, base=2, close=3000.0, ma20=None),
    "主线破位": dict(gate_hit=False, gate_blocked=True, ml_breakdown=True, bull=True, base=3),
    "主线未成立": dict(gate_hit=False, gate_blocked=True, state="none", base=0),
    "主线成立弱": dict(gate_hit=False, gate_blocked=False, state="established", strength="weak", bull=True, base=3),
    "主线成立强被压制": dict(gate_hit=True, gate_blocked=True, state="established", strength="strong", base=0),
    "主线成立强无压制": dict(gate_hit=False, gate_blocked=False, state="established", strength="strong", bull=True, base=3, adj=1, final=4),
    "主线数据不可用": dict(gate_hit=False, gate_blocked=True, state="unavailable", base=0),
    "事件窗口内": dict(gate_hit=False, gate_blocked=False, state="none", event_d=2, base=2),
    "事件当天未落档": dict(gate_hit=False, gate_blocked=False, state="none", event_d=0, base=2),
    "事件当天已落档": dict(gate_hit=False, gate_blocked=False, state="none", event_d=0, event_result="超预期", base=2),
    "事件窗口外": dict(gate_hit=False, gate_blocked=False, state="none", event_d=9, base=2),
    "宽度偏强": dict(gate_hit=False, gate_blocked=False, state="none", base=2, breadth={"advance_ratio": 0.72}),
    "宽度偏弱": dict(gate_hit=False, gate_blocked=False, state="none", base=2, breadth={"advance_ratio": 0.2}),
    "宽度缺失": dict(gate_hit=False, gate_blocked=False, state="none", base=2, breadth=None),
}


@pytest.mark.parametrize("name,kwargs", LEDGER_CASES.items())
def test_all_ledger_copy_passes_guard(name, kwargs):
    """账本三项文案全分支过门禁：一行都不许把工程词漏到用户面前。"""
    ledger = _ledger(**kwargs)
    for item in ledger.items:
        for field in (item.effect, item.judgement, item.value_text):
            assert find_engineering_terms(field) == [], f"{name}/{item.key} 命中工程词：{field!r}"
