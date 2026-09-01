from datetime import datetime

import pytest

from aistock_agent.schemas.target import Target
from aistock_agent.skills.scene_probe import match_scenarios, probe_scene_confirmation


def test_match_scenarios_hit_and_miss() -> None:
    conclusion = "本轮上行主因是降息预期兑现与流动性宽松共振"
    matched = match_scenarios(conclusion, ["降息预期兑现", "地缘政治缓和", "流动性宽松"])
    assert "降息预期兑现" in matched
    assert "流动性宽松" in matched
    assert "地缘政治缓和" not in matched


def test_match_scenarios_empty_and_blank() -> None:
    assert match_scenarios("", ["a"]) == []
    assert match_scenarios("无结论", []) == []


@pytest.mark.asyncio
async def test_probe_uses_injected_predictions() -> None:
    target = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    rec = {
        "prediction": {
            "horizons": [{"target": "上证指数"}],
            "conditions": [{"scenario": "降息预期兑现", "direction": "up"}],
        },
        "due_dates": {},
        "verification": {},
    }
    got = await probe_scene_confirmation(
        target=target,
        trace_id="tr1",
        conclusion="上行主因是降息预期兑现与流动性宽松共振",
        fetched_predictions=[rec],
    )
    assert len(got) == 1
    assert got[0].confirmed_kind == "scene_match"
    assert got[0].source_trace_id == "tr1"


@pytest.mark.asyncio
async def test_probe_no_match_returns_empty() -> None:
    target = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    rec = {
        "prediction": {"horizons": [{"target": "上证指数"}], "conditions": [{"scenario": "地缘政治缓和"}]},
        "due_dates": {},
        "verification": {},
    }
    got = await probe_scene_confirmation(
        target=target, trace_id="tr2", conclusion="上行主因是降息预期兑现", fetched_predictions=[rec]
    )
    assert got == []


@pytest.mark.asyncio
async def test_probe_skips_other_target() -> None:
    target = Target(kind="index", internal_id="000001.SH", code="000001.SH", name="上证指数")
    rec = {"prediction": {"horizons": [{"target": "半导体板块"}], "conditions": [{"scenario": "降息预期兑现"}]}}
    got = await probe_scene_confirmation(
        target=target, trace_id="tr3", conclusion="降息预期兑现", fetched_predictions=[rec]
    )
    assert got == []