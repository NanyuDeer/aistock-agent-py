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


def test_synthesis_prompt_pins_enums_and_anchor_guard():
    ev = RhythmEvidence(stage="ebb", certainty="low")  # 无 event_anchors
    prompt = build_synthesis_prompt(ev)
    # G5：Literal 字段必须显式列出可选值
    assert "bullish" in prompt and "bearish" in prompt and "neutral" in prompt
    assert "high" in prompt and "medium" in prompt and "low" in prompt
    # 无锚点时明确要求空数组，禁止占位方向
    assert "launch_outlook" in prompt
    assert "[]" in prompt
    assert "无 high 事件锚点" in prompt
    # 语义方向锁定：无 high 锚点 → 空数组（不得反向诱导填充）
    assert "不是「无 high 事件锚点」" in prompt
