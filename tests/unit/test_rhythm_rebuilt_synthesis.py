from aistock_agent.schemas.rhythm_master import RhythmEvidence
from aistock_agent.prompts.workers.rhythm_master import build_synthesis_prompt


def test_prompt_contains_evidence_and_constraints():
    ev = RhythmEvidence(stage="launch", certainty="medium")
    prompt = build_synthesis_prompt(ev)
    assert "启动" in prompt or "launch" in prompt
    assert "假设推演" in prompt
    assert "不输出点位" in prompt


def test_synthesis_prompt_contains_json_and_schema_fields():
    ev = RhythmEvidence(stage="rally", certainty="high")
    prompt = build_synthesis_prompt(ev)
    assert "json" in prompt.lower()
    assert "mainline" in prompt
    assert "launch_outlook" in prompt
    assert "narrative" in prompt
