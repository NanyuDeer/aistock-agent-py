"""锁定 midday 生成契约（schema 2.1，2026-09-03）：
午后前瞻 = opportunities 关键词（4-5 个、每个 ≤8 字，无 conclusion 段落）；risks 收紧为短词。
"""

from aistock_agent.agents.workers.midday import _build_midday_report
from aistock_agent.prompts.workers.midday import MIDDAY_PROMPT


def test_prompt_requires_opportunities_keywords_for_afternoon_outlook():
    # 午后前瞻分段：要求 opportunities 关键词（≤8 字 4-5 个），示例不再含 conclusion
    assert '"title": "午后前瞻"' in MIDDAY_PROMPT
    assert '"opportunities"' in MIDDAY_PROMPT
    assert "每个 ≤8 字" in MIDDAY_PROMPT
    assert "共 4-5 个" in MIDDAY_PROMPT
    # 负向锁定：午后前瞻 JSON 示例不含 conclusion（机会关键词契约，段落留 details 详述）
    assert '"title": "午后前瞻", "conclusion"' not in MIDDAY_PROMPT
    assert '"schema_version": "2.1"' in MIDDAY_PROMPT


def test_prompt_requires_short_risk_words():
    # risks 输出收紧为 ≤8 字短词 4-5 条（对称）
    assert '"risks": [' in MIDDAY_PROMPT
    assert "≤8" in MIDDAY_PROMPT or "8 字" in MIDDAY_PROMPT


def test_build_midday_report_bumps_schema_and_passes_opportunities_through():
    display = {
        "summary": "结论",
        "sections": [
            {"title": "上午盘面回顾", "conclusion": "回顾"},
            {"title": "午后前瞻", "opportunities": ["AI算力", "低空经济"]},
        ],
        "details": "详情",
        "stocks": [],
        "risks": ["高位股回调"],
    }
    report = _build_midday_report(display, "", podcast_brief="")
    assert report["schema_version"] == "2.1"
    sections = report["display_report"]["sections"]
    assert sections[1]["opportunities"] == ["AI算力", "低空经济"]
    assert report["display_report"]["risks"] == ["高位股回调"]
