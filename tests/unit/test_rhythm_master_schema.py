# tests/unit/test_rhythm_master_schema.py
from aistock_agent.schemas.rhythm_master import MasterRhythmCard, RhythmEvidence, RhythmSynthesis

def test_evidence_defaults():
    ev = RhythmEvidence()
    assert ev.stage is None
    assert ev.certainty is None
    assert ev.position is None
    assert ev.event_anchors == []
    assert ev.data_missing == []

def test_card_builds_from_evidence_and_synthesis():
    ev = RhythmEvidence(stage="rally", certainty="high")
    synth = RhythmSynthesis(mainline=[], launch_outlook=[], narrative="主线共振，主升阶段。")
    card = MasterRhythmCard(
        basis_date="2026-09-03", target_date="2026-09-04", refresh_slot="after_close",
        evidence=ev, synthesis=synth, synthesis_available=True,
    )
    assert card.synthesis is not None
    assert card.synthesis_available is True
    assert card.evidence.stage == "rally"
