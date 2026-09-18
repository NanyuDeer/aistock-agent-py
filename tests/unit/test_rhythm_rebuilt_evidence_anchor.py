# tests/unit/test_rhythm_rebuilt_evidence_anchor.py
from aistock_agent.services.rhythm_rebuilt_evidence import build_event_anchors

def test_anchors_only_high_importance():
    events = [
        {"date": "2026-09-08", "title": "CPI 公布", "importance": "high"},
        {"date": "2026-09-09", "title": "某公司财报", "importance": "medium"},
    ]
    anchors = build_event_anchors(events)
    assert len(anchors) == 1
    assert anchors[0].title == "CPI 公布"
    assert anchors[0].confirm_condition == "公布后按预期差确认"

def test_empty_events_gives_empty_anchors():
    assert build_event_anchors([]) == []
