import pytest

from aistock_agent.services.prediction_stats import build_scenario_harvest, build_validation_profile


def _conf(pid: str, scenario: str) -> dict[str, object]:
    return {"prediction_id": pid, "scenario": scenario, "source_trace_id": "t1",
            "confirmed_kind": "scene_match", "confirmed_at": "2026-09-01T00:00:00Z"}


def test_build_scenario_harvest_counts_by_scenario() -> None:
    confirmations = [_conf("p1", "降息预期兑现"), _conf("p2", "降息预期兑现"), _conf("p1", "流动性宽松")]
    harvest = build_scenario_harvest(confirmations)
    assert harvest["confirmed"]["降息预期兑现"] == 2
    assert harvest["confirmed"]["流动性宽松"] == 1
    assert harvest["unconfirmed"] == {}


def test_build_scenario_harvest_empty() -> None:
    assert build_scenario_harvest([]) == {"confirmed": {}, "unconfirmed": {}}


def test_profile_adds_channel_b_fields() -> None:
    confirmations = [_conf("p1", "降息预期兑现")]
    profile = build_validation_profile([], target="000001.SH", confirmations=confirmations)
    assert profile["evidence_confirmed"] == confirmations
    assert profile["scenario_harvest"]["confirmed"] == {"降息预期兑现": 1}


def test_profile_channel_b_default_absent() -> None:
    profile = build_validation_profile([], target="000001.SH")
    assert profile.get("evidence_confirmed") == []
    assert profile.get("scenario_harvest") == {"confirmed": {}, "unconfirmed": {}}