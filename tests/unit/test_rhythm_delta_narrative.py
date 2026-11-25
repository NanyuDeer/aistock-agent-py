import pytest

from aistock_agent.schemas.rhythm_master import (
    DeltaBlocking,
    DeltaGap,
    DeltaPosition,
    DeltaRef,
    DeltaReview,
    RhythmDelta,
)
from aistock_agent.services.rhythm_delta_narrative import fallback_text, validate_narrative
from aistock_agent.services.user_facing_copy import check_copy, find_engineering_terms


def _delta(**overrides) -> RhythmDelta:
    base = dict(
        prev=DeltaRef(target_date="2026-11-05", slot="after_close", lag_days=1),
        position=DeltaPosition(from_index=0, to_index=0, changed=False),
        blocking=DeltaBlocking(resolved=[], added=[], persisting=["trend", "mainline"]),
        gaps=[DeltaGap(key="gate", from_gap=21.53, to_gap=11.62, unit="点")],
        default_key="gate",
    )
    base.update(overrides)
    return RhythmDelta(**base)


def test_rejects_fabricated_number() -> None:
    ok, _ = validate_narrative(_delta(), {"main_key": "gate", "text": "离加仓还差 99.9 点"})
    assert ok is False


def test_accepts_number_from_delta_with_tolerance() -> None:
    ok, _ = validate_narrative(_delta(), {"main_key": "gate", "text": "离加仓还差 11.62 点"})
    assert ok is True


def test_rejects_banned_engineering_term() -> None:
    ok, reason = validate_narrative(_delta(), {"main_key": "gate", "text": "闸门仍卡着，仓位不变"})
    assert ok is False and "工程词" in reason


def test_allows_business_words() -> None:
    """反向用例：主线/超额/均线是炒股词，必须放行（禁的是计算机语言，不是炒股语言）。"""
    ok, _ = validate_narrative(_delta(), {"main_key": "gate", "text": "主线仍弱，仓位维持空仓观望"})
    assert ok is True


def test_rejects_invalid_main_key() -> None:
    ok, _ = validate_narrative(_delta(), {"main_key": "trand", "text": "仓位维持空仓观望"})
    assert ok is False


def test_rejects_key_not_present_in_delta() -> None:
    """main_key 必须是差异数据里真实出现过的键：没有任何 event 距离变化却声称 event。"""
    d = _delta(gaps=[DeltaGap(key="gate", from_gap=21.53, to_gap=11.62, unit="点")],
               blocking=DeltaBlocking())
    ok, _ = validate_narrative(d, {"main_key": "event", "text": "离重要事件更近了"})
    assert ok is False


def test_rejects_imperative_words() -> None:
    ok, _ = validate_narrative(_delta(), {"main_key": "gate", "text": "建议满仓"})
    assert ok is False


def test_rejects_overlong_text() -> None:
    ok, _ = validate_narrative(_delta(), {"main_key": "gate", "text": "仓" * 51})
    assert ok is False


def test_disclaimer_is_stripped_not_fatal() -> None:
    """含「不构成投资建议」不算失败：剥离后保留正文（避免一次沟通成本换整句作废）。"""
    ok, _ = validate_narrative(
        _delta(), {"main_key": "gate", "text": "仓位维持空仓观望，不构成投资建议"},
    )
    assert ok is True


def test_allows_number_from_review() -> None:
    """复盘数字（指数涨跌幅）也是 delta 里出现过的数字，必须放行。"""
    d = _delta(review=DeltaReview(
        prev_target_date="2026-11-05", prev_position_text="空仓观望",
        index_pct_chg=-0.82, up_ratio=0.31,
    ))
    ok, _ = validate_narrative(d, {"main_key": "gate", "text": "昨日指数跌 0.82 个百分点"})
    assert ok is True


@pytest.mark.parametrize("key", ["position", "gate", "mainline", "event", "none"])
def test_fallback_text_never_empty_and_passes_copy_guard(key: str) -> None:
    text = fallback_text(_delta(default_key=key))
    assert text.strip() != ""
    assert find_engineering_terms(text) == [], text
    check_copy(text)


def test_fallback_text_for_no_change() -> None:
    assert fallback_text(_delta(default_key="none")) == "和上一条比没有实质变化，仓位和卡住的原因都没动"


@pytest.mark.asyncio
async def test_run_falls_back_when_llm_raises(monkeypatch) -> None:
    """LLM 调用异常不得冒泡：一律落模板句（永不空、不抛）。"""
    from aistock_agent.services import rhythm_delta_narrative as mod

    class _Boom:
        async def ainvoke(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(mod, "get_deep_think", lambda: _Boom())
    text, degraded = await mod.run(_delta())
    assert text == fallback_text(_delta())
    assert degraded is True   # 调用异常 = LLM 层真没工作 → 调用方要留痕


@pytest.mark.asyncio
async def test_run_falls_back_when_output_is_not_json(monkeypatch) -> None:
    """模型输出解析不出 JSON → 校验判失败 → 落模板句。"""
    from aistock_agent.services import rhythm_delta_narrative as mod

    class _Resp:
        content = "我认为主要矛盾是条件还差一点"

    class _Llm:
        async def ainvoke(self, *_args: object, **_kwargs: object) -> object:
            return _Resp()

    monkeypatch.setattr(mod, "get_deep_think", lambda: _Llm())
    text, degraded = await mod.run(_delta())
    assert text == fallback_text(_delta())
    assert degraded is True   # 解析不出 JSON 同样视为没工作


def test_allows_descriptive_position_words() -> None:
    """「离加仓还差 X 点」是描述性说法（spec §7.5 模板原句），不是交易指令，必须放行。"""
    ok, _ = validate_narrative(_delta(), {"main_key": "gate", "text": "离加仓还差 11.62 点"})
    assert ok is True
