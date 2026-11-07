from aistock_agent.schemas.rhythm_master import (
    LaunchOutlook,
    RhythmEvidence,
    RhythmSynthesis,
)
from aistock_agent.services.rhythm_rebuilt_validate import validate_synthesis


def _outlook(**kwargs):
    base = dict(
        anchor_date="2026-09-08", title="CPI 公布",
        if_confirmed_direction="bullish", confidence="medium",
    )
    base.update(kwargs)
    return LaunchOutlook(**base)


def test_valid_outlook_passes():
    ev = RhythmEvidence(stage="rally", certainty="high")
    synth = RhythmSynthesis(launch_outlook=[_outlook()])
    assert validate_synthesis(synth, ev)


def test_reject_missing_confidence():
    synth = RhythmSynthesis(launch_outlook=[_outlook(confidence="maybe")])
    assert not validate_synthesis(synth, RhythmEvidence())


def test_reject_bad_direction():
    # Literal 无法直接构造非法值：绕过 pydantic 模拟线上/上游原始输出
    synth = RhythmSynthesis(
        launch_outlook=[LaunchOutlook.model_construct(
            anchor_date="2026-09-08", title="CPI",
            if_confirmed_direction="待定", confidence="medium",
        )]
    )
    assert not validate_synthesis(synth, RhythmEvidence())


def test_reject_all_empty_synthesis():
    synth = RhythmSynthesis(launch_outlook=[])
    assert not validate_synthesis(synth, RhythmEvidence())


def test_synthesis_has_no_narrative_field() -> None:
    from aistock_agent.schemas.rhythm_master import RhythmSynthesis

    assert "narrative" not in RhythmSynthesis.model_fields
    assert "mainline" not in RhythmSynthesis.model_fields


def test_validate_accepts_empty_outlook() -> None:
    """三个字段全空 = 空壳 → 校验为 False（正常路径，不是异常）。"""
    from aistock_agent.schemas.rhythm_master import RhythmEvidence, RhythmSynthesis
    from aistock_agent.services.rhythm_rebuilt_validate import validate_synthesis

    s = RhythmSynthesis(launch_outlook=[])
    assert validate_synthesis(s, RhythmEvidence()) is False
