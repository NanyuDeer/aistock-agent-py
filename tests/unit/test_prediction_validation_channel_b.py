"""P1：渠道B 回扫优先读归因结论层（root）的 confirmed_prediction。"""
from aistock_agent.skills.prediction_validation import _extract_root_confirmed


def test_extract_root_confirmed_reads_root_list():
    trace = {
        "confirmed_prediction": [
            {"prediction_id": "183", "scenario": "量能萎缩后回踩",
             "source_trace_id": "review:2026-10-06", "confirmed_kind": "scene_match"},
        ]
    }
    out = _extract_root_confirmed(trace)
    assert len(out) == 1 and out[0]["prediction_id"] == "183"


def test_extract_root_confirmed_tolerates_missing_or_dirty():
    assert _extract_root_confirmed({}) == []
    assert _extract_root_confirmed({"confirmed_prediction": None}) == []
    assert _extract_root_confirmed({"confirmed_prediction": ["bad"]}) == []
