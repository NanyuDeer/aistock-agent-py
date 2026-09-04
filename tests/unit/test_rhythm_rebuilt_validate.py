from aistock_agent.schemas.rhythm_master import RhythmSynthesis, LaunchOutlook, MainlineRef, RhythmEvidence
from aistock_agent.services.rhythm_rebuilt_validate import validate_synthesis, _contains_price_point

def test_reject_price_point_in_narrative():
    assert _contains_price_point("预计目标点位 3800 点")
    assert not _contains_price_point("主线为券商板块，主升阶段")

def test_valid_synthesis_passes():
    ev = RhythmEvidence(stage="rally", certainty="high")
    synth = RhythmSynthesis(
        mainline=[MainlineRef(name="券商", stage="主升", source="wind_leader", data_date="2026-09-03", direction="bullish", confidence="high")],
        launch_outlook=[LaunchOutlook(anchor_date="2026-09-08", title="CPI 公布", if_confirmed_direction="bullish", confidence="medium")],
        narrative="主线券商共振，主升阶段，但属假设推演，不构成投资建议。",
    )
    assert validate_synthesis(synth, ev)

def test_reject_missing_confidence():
    synth = RhythmSynthesis(
        mainline=[],
        launch_outlook=[LaunchOutlook(anchor_date="2026-09-08", title="CPI", if_confirmed_direction="bullish", confidence="maybe")],
        narrative="展望",
    )
    assert not validate_synthesis(synth, RhythmEvidence())

def test_reject_synthesis_not_grounded_on_evidence():
    synth = RhythmSynthesis(
        mainline=[MainlineRef(name="芯片", stage="主升", source="", data_date="", direction="bullish", confidence="high")],
        launch_outlook=[],
        narrative="",
    )
    assert not validate_synthesis(synth, RhythmEvidence())
