# tests/unit/test_rhythm_master_schema.py
from aistock_agent.schemas.rhythm_master import (
    STAGE_TO_LEVEL,
    MasterRhythmCard,
    RhythmEvidence,
    RhythmSynthesis,
)


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

def test_stage_to_level_mapping_matches_frontend_five_levels():
    assert STAGE_TO_LEVEL["ice"] == {"level": "ice", "score": 0}
    assert STAGE_TO_LEVEL["ebb"] == {"level": "low", "score": 20}
    assert STAGE_TO_LEVEL["launch"] == {"level": "normal", "score": 40}
    assert STAGE_TO_LEVEL["rally"] == {"level": "active", "score": 60}
    assert STAGE_TO_LEVEL["overheat"] == {"level": "euphoria", "score": 80}
    assert STAGE_TO_LEVEL[None] is None
