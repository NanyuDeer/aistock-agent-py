# tests/unit/test_rhythm_rebuilt_evidence_certainty.py
from aistock_agent.services.rhythm_rebuilt_evidence import detect_certainty


def test_high_certainty_on_event_and_volume_confirm():
    cert, _ = detect_certainty(
        event_confirm=True, volume_direction="bullish", stage="rally",
        breadth={"advance_count": 3800, "decline_count": 900, "total_count": 5000},
    )
    assert cert == "high"

def test_low_certainty_when_nothing_confirms():
    cert, _ = detect_certainty(
        event_confirm=False, volume_direction=None, stage="ice",
        breadth={"advance_count": 800, "decline_count": 3900, "total_count": 5000},
    )
    assert cert == "low"
