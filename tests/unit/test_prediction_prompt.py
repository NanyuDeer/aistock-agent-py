from aistock_agent.prompts.workers.prediction import PREDICTION_PROMPT


def test_prompt_covers_three_horizons():
    for key in ("short", "mid", "long"):
        assert key in PREDICTION_PROMPT


def test_prompt_forbids_fabrication():
    assert "evidence_ids" in PREDICTION_PROMPT
    assert "禁止" in PREDICTION_PROMPT


def test_prompt_requires_mechanism_on_direction_switch():
    assert "切换" in PREDICTION_PROMPT
    assert "驱动力" in PREDICTION_PROMPT
