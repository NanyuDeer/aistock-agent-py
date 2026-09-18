from aistock_agent.schemas.rhythm_master import MainlineRef, RhythmEvidence, RhythmSynthesis
from aistock_agent.services.rhythm_rebuilt_validate import prune_invalid, validate_synthesis

EVID = RhythmEvidence(stage="launch", stage_reason="x", certainty="medium", certainty_reason="x")


def _mk(name="AI 算力", source="ths_daily", data_date="2026-09-12"):
    return MainlineRef(name=name, stage="launch", source=source, data_date=data_date,
                       direction="bullish", confidence="medium")


def test_llm_mainline_not_in_candidates_pruned():
    syn = RhythmSynthesis(mainline=[_mk(name="编造的新主线")], launch_outlook=[], narrative="x")
    out = prune_invalid(syn, candidate_names={"AI 算力", "低空经济"})
    assert out.mainline == []


def test_llm_mainline_wrong_data_date_pruned():
    syn = RhythmSynthesis(mainline=[_mk(data_date="2026-09-09")], launch_outlook=[], narrative="x")
    out = prune_invalid(syn, candidate_names={"AI 算力"}, evidence_date="2026-09-12")
    assert out.mainline == []


def test_valid_mainline_kept():
    syn = RhythmSynthesis(mainline=[_mk()], launch_outlook=[], narrative="x")
    out = prune_invalid(syn, candidate_names={"AI 算力"}, evidence_date="2026-09-12")
    assert len(out.mainline) == 1 and out.mainline[0].name == "AI 算力"


def test_validate_rejects_wrong_name():
    syn = RhythmSynthesis(mainline=[_mk(name="编造")], launch_outlook=[], narrative="x")
    assert validate_synthesis(syn, EVID, candidate_names={"AI 算力"}) is False


def test_validate_rejects_wrong_data_date():
    syn = RhythmSynthesis(mainline=[_mk(data_date="2026-09-09")], launch_outlook=[], narrative="x")
    assert validate_synthesis(syn, EVID, candidate_names={"AI 算力"},
                              evidence_date="2026-09-12") is False


def test_validate_accepts_valid_mainline():
    syn = RhythmSynthesis(mainline=[_mk()], launch_outlook=[], narrative="x")
    assert validate_synthesis(syn, EVID, candidate_names={"AI 算力"},
                              evidence_date="2026-09-12") is True
