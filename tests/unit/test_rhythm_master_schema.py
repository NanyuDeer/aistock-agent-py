# tests/unit/test_rhythm_master_schema.py
import pytest

from aistock_agent.schemas.rhythm_master import (
    STAGE_TO_LEVEL,
    GapValue,
    MasterRhythmCard,
    RhythmEvidence,
    RhythmSynthesis,
    WatchPoint,
)


def test_evidence_defaults():
    ev = RhythmEvidence()
    assert ev.stage is None
    assert ev.certainty is None
    assert ev.position is None
    assert ev.position_ledger is None
    assert ev.event_anchors == []
    assert ev.data_missing == []

def test_card_builds_from_evidence_and_synthesis():
    ev = RhythmEvidence(stage="rally", certainty="high")
    synth = RhythmSynthesis(launch_outlook=[])
    card = MasterRhythmCard(
        basis_date="2026-09-03", target_date="2026-09-04", refresh_slot="after_close",
        evidence=ev, synthesis=synth, synthesis_available=False,
    )
    assert card.synthesis is not None
    # 本批 LLM 复述层（narrative/mainline）已下线，launch_outlook 恒空 → 不可用
    assert card.synthesis_available is False
    assert card.evidence.stage == "rally"

def test_stage_to_level_mapping_matches_frontend_five_levels():
    assert STAGE_TO_LEVEL["ice"] == {"level": "ice", "score": 0}
    assert STAGE_TO_LEVEL["ebb"] == {"level": "low", "score": 20}
    assert STAGE_TO_LEVEL["launch"] == {"level": "normal", "score": 40}
    assert STAGE_TO_LEVEL["rally"] == {"level": "active", "score": 60}
    assert STAGE_TO_LEVEL["overheat"] == {"level": "euphoria", "score": 80}
    assert STAGE_TO_LEVEL[None] is None


def test_watch_point_gap_optional_and_validated():
    wp = WatchPoint(
        key="gate", condition="x", current_text="y", sufficient_alone=False,
        gap=GapValue(
            key="gate", current=3689.91, target=3701.53, gap=11.62,
            unit="点", direction="up",
        ),
    )
    assert wp.gap is not None and wp.gap.gap == pytest.approx(11.62)

    legacy = WatchPoint(key="gate", condition="x", current_text="y", sufficient_alone=False)
    assert legacy.gap is None


def test_gap_value_rejects_unknown_unit():
    with pytest.raises(Exception):
        GapValue(key="gate", current=1.0, target=2.0, gap=1.0, unit="元", direction="up")


def test_rhythm_delta_roundtrip() -> None:
    from aistock_agent.schemas.rhythm_master import (
        DeltaBlocking,
        DeltaPosition,
        DeltaRef,
        RhythmDelta,
    )

    d = RhythmDelta(
        prev=DeltaRef(target_date="2026-11-05", slot="after_close", created_at=None, lag_days=1),
        position=DeltaPosition(from_index=1, to_index=0, changed=True),
        stage={"from": "ebb", "to": "overheat"},
        blocking=DeltaBlocking(resolved=[], added=["trend"], persisting=["mainline"]),
        default_key="gate",
    )
    dumped = d.model_dump()
    assert RhythmDelta.model_validate(dumped).default_key == "gate"
