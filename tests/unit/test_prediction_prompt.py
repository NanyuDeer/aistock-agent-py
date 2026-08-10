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


def test_prompt_requires_structured_evolution_steps():
    # B2 前端时间轴：演化路径须输出结构化步骤（label+text 按档位切分），而非仅一段叙事
    assert "evolution_steps" in PREDICTION_PROMPT
    assert "label" in PREDICTION_PROMPT
    assert "text" in PREDICTION_PROMPT
