# tests/unit/test_rhythm_rebuilt_evidence_stage.py
from aistock_agent.services.rhythm_rebuilt_evidence import detect_stage

def test_rally_when_breadth_up_volume_up_and_ma_bullish():
    breadth = {"advance_count": 3800, "decline_count": 900, "total_count": 5000}
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
              110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120]
    amounts = [5000] * 20
    sentiment = [30, 35, 40, 45, 50, 55]
    stage, _ = detect_stage(
        breadth=breadth, closes=closes, amounts=amounts,
        sentiment_scores=sentiment, fg=65, prev_phase=None,
    )
    assert stage == "rally"

def test_ice_when_breadth_down_volume_weak():
    breadth = {"advance_count": 800, "decline_count": 3900, "total_count": 5000}
    closes = [120, 119, 118, 117, 116, 115, 114, 113, 112, 111,
              110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
    amounts = [2000] * 20
    sentiment = [40, 35, 30, 25, 20, 15]
    stage, _ = detect_stage(
        breadth=breadth, closes=closes, amounts=amounts,
        sentiment_scores=sentiment, fg=20, prev_phase=None,
    )
    assert stage == "ice"
