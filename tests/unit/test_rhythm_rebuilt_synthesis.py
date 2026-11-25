from aistock_agent.prompts.workers.rhythm_master import build_synthesis_prompt
from aistock_agent.schemas.rhythm_master import RhythmEvidence


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
    assert "launch_outlook" in prompt


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


def test_prune_invalid_drops_only_bad_elements():
    from aistock_agent.schemas.rhythm_master import LaunchOutlook, RhythmSynthesis
    from aistock_agent.services.rhythm_rebuilt_validate import prune_invalid

    ok_out = LaunchOutlook(anchor_date="2026-09-18", title="交割日",
                           if_confirmed_direction="neutral", confidence="low")
    # Literal 无法直接构造非法值：绕过 pydantic 模拟上游原始输出 → 应被剔除
    bad_out = LaunchOutlook.model_construct(
        anchor_date="2026-09-19", title="臆造",
        if_confirmed_direction="待定", confidence="high",
    )
    syn = RhythmSynthesis(launch_outlook=[ok_out, bad_out])
    pruned = prune_invalid(syn)
    assert len(pruned.launch_outlook) == 1
    assert pruned.launch_outlook[0].title == "交割日"


def test_prompt_includes_mainline_direction_label():
    ev = RhythmEvidence(stage="launch")
    facts = {"state": "established", "name": "液冷服务器", "strength": "strong",
             "excess": 5.2, "data_date": "2026-09-18", "mainline_label": "AI硬件"}
    prompt = build_synthesis_prompt(ev, facts)
    assert "AI硬件方向（液冷服务器" in prompt
    assert "液冷服务器（strength=strong" not in prompt
    assert "不得自创主线" in prompt
    assert "mainline 必须为 []" in prompt
    assert "主线段只能引用上方「确定性主线事实」中的候选" in prompt


def test_prompt_keeps_legacy_format_without_label():
    ev = RhythmEvidence(stage="launch")
    facts = {"state": "established", "name": "液冷服务器", "strength": "strong",
             "excess": 5.2, "data_date": "2026-09-18"}
    prompt = build_synthesis_prompt(ev, facts)
    assert "确定性主线事实：液冷服务器（strength=strong" in prompt
    assert "方向（" not in prompt
