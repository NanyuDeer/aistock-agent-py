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


CAUSALITY_CASES = {
    "双条件卡着": dict(gate_hit=True, gate_blocked=True, state="none", base=0),
    "只差主线": dict(gate_hit=False, gate_blocked=True, state="none", base=0),
    "主线破位": dict(gate_hit=False, gate_blocked=True, ml_breakdown=True, bull=True, base=3),
    "无阻塞正向": dict(gate_hit=False, gate_blocked=False, state="established", strength="strong", bull=True, base=3, adj=1, final=4),
    "事件封顶": dict(gate_hit=False, gate_blocked=False, state="none", event_d=0, base=2),
}


@pytest.mark.parametrize("name,kwargs", CAUSALITY_CASES.items())
def test_causality_copy_passes_guard(name, kwargs):
    """溯源主区文案（lead + lines）全分支过门禁。"""
    from aistock_agent.services.rhythm_position_ledger import render_causality

    text = render_causality(_ledger(**kwargs))
    for piece in [text.lead, *text.lines]:
        assert find_engineering_terms(piece) == [], f"{name} 命中工程词：{piece!r}"


def test_watch_point_copy_passes_guard():
    """变奏观察点 condition / current_text 全分支过门禁。"""
    from aistock_agent.schemas.rhythm_master import CandidateOutlook
    from aistock_agent.services.rhythm_position_ledger import build_watch_points

    closes = [3600.0] * 19 + [3689.91]
    candidates = [
        CandidateOutlook(name="AI 应用", excess=0.3),
        CandidateOutlook(name="低空经济", excess=-1.13),
        CandidateOutlook(name="液冷服务器", excess=-4.85),
    ]
    cases = [
        dict(gate_hit=True, gate_blocked=True, state="none", base=0),
        dict(gate_hit=False, gate_blocked=True, ml_breakdown=True, bull=True, base=3),
        dict(gate_hit=False, gate_blocked=True, state="unavailable", base=0),
        dict(gate_hit=False, gate_blocked=False, state="none", base=2),
        dict(gate_hit=False, gate_blocked=False, state="none", base=2, event_d=0),
        dict(gate_hit=False, gate_blocked=False, state="none", base=2, event_d=9),
    ]
    for kwargs in cases:
        ledger = _ledger(**kwargs)
        for wp in build_watch_points(
            ledger, mainline_facts={"name": "AI 应用", "strength": "weak"},
            event_d=kwargs.get("event_d"), closes=closes, candidates=candidates,
        ):
            for field in (wp.condition, wp.current_text):
                assert find_engineering_terms(field) == [], f"{wp.key} 命中工程词：{field!r}"


def test_causality_keeps_stock_words():
    """L1 炒股词（主线/超额/均线）必须放行——禁的是计算机语言，不是炒股语言。"""
    from aistock_agent.services.rhythm_position_ledger import render_causality

    text = render_causality(_ledger(gate_hit=False, gate_blocked=True, state="none", base=0))
    joined = " ".join([text.lead, *text.lines])
    assert "主线" in joined or "超额" in joined
    assert find_engineering_terms(joined) == []


def test_mainline_notes_pass_guard():
    """主线留痕文案全分支过门禁，且四类根因仍可区分（硬约束 12）。"""
    from aistock_agent.services.mainline_engine import build_mainline_notes
    from aistock_agent.services.user_facing_copy import find_engineering_terms

    combos = [
        dict(valid_count=3, min_candidates=3, code_skipped=0, name_skipped=0, thin_skipped=0, fetch_failed=0),
        dict(valid_count=2, min_candidates=3, code_skipped=1, name_skipped=2, thin_skipped=3, fetch_failed=0),
        dict(valid_count=0, min_candidates=3, code_skipped=0, name_skipped=1, thin_skipped=0, fetch_failed=4),
        dict(valid_count=3, min_candidates=3, code_skipped=0, name_skipped=0, thin_skipped=0, fetch_failed=0, unscorable=2),
    ]
    for kwargs in combos:
        for note in build_mainline_notes(**kwargs):
            assert find_engineering_terms(note) == [], f"命中工程词：{note!r}"

    merged = " ".join(build_mainline_notes(**combos[2]))
    assert "取不到" in merged and "不够用" in merged  # 取数失败与数据不足不得合并


def test_data_missing_has_no_legacy_jargon():
    """data_missing / block_reason 的存量工程语不得回潮（源码级守卫）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "aistock_agent"
    legacy = [
        "基准卡", "收盘基准", "主档位", "主阶段",
        "档位由", "档位已在下限", "命中规则", "封顶至",
        "data_date=", "证据日=", "宽度快照", "分支生成降级",
        "候选不可评分", "主线候选不可用", "主线候选取数失败",
        "名称校验不通过", "事件当期未落档", "继承序号",
        "d 不可用", "无事件 / ",
    ]
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in legacy:
            if word in text:
                offenders.append(f"{path.name}: {word}")
    assert offenders == [], f"仍存在工程语：{offenders}"


def test_causality_joined_sentence_has_no_broken_punctuation():
    """lead + lines 是按「；」拼接上屏的：lead 以冒号收尾会拼出「：；」，读起来断掉。"""
    from aistock_agent.services.rhythm_position_ledger import render_causality

    for name, kwargs in CAUSALITY_CASES.items():
        text = render_causality(_ledger(**kwargs))
        joined = "；".join([text.lead, *text.lines])
        assert "：；" not in joined, f"{name}: {joined}"
        assert "；；" not in joined, f"{name}: {joined}"
        assert not joined.startswith("；"), f"{name}: {joined}"


def test_cheng_numbers_use_chinese_numerals():
    """仓位成数一律中文数字：「两成」不是「2 成」（同一张卡里不许两种写法并存）。"""
    for kwargs in LEDGER_CASES.values():
        for item in _ledger(**kwargs).items:
            assert " 2 成" not in item.effect, item.effect
            assert " 3 成" not in item.effect, item.effect
            # 中文数字与「成」之间不留空格：「按两成打底」不是「按 两 成打底」
            assert " 成" not in item.effect, item.effect


@pytest.mark.parametrize(
    "key,pos",
    [
        ("position", dict(from_index=1, to_index=0, changed=True)),
        ("gate", dict(from_index=0, to_index=0)),
        ("mainline", dict(from_index=0, to_index=0)),
        ("event", dict(from_index=0, to_index=0)),
        ("none", dict(from_index=0, to_index=0)),
    ],
)
def test_fallback_templates_pass_copy_guard(key, pos):
    """所有兜底模板都必须过 §12.6 黑名单（含各 default_key 分支）。"""
    from aistock_agent.schemas.rhythm_master import (
        DeltaBlocking, DeltaGap, DeltaPosition, DeltaRef, RhythmDelta,
    )
    from aistock_agent.services.rhythm_delta_narrative import fallback_text

    delta = RhythmDelta(
        prev=DeltaRef(target_date="2026-11-05", slot="after_close"),
        position=DeltaPosition(**pos),
        blocking=DeltaBlocking(),
        gaps=[DeltaGap(key="gate", from_gap=21.53, to_gap=11.62, unit="点")],
        default_key=key,
    )
    text = fallback_text(delta)
    assert text.strip() != ""
    check_copy(text)
