"""midday 报告组装：机会/风险由代码侧候选集覆写（schema 2.1）。"""

from aistock_agent.agents.workers import midday as m


def _display(sections: list[dict], risks: list[str]) -> dict:
    return {
        "summary": "一句话",
        "sections": sections,
        "details": "完整内容",
        "stocks": [],
        "risks": risks,
    }


def test_build_midday_report_overrides_opportunities_and_risks():
    display = _display(
        [
            {"title": "上午盘面回顾", "conclusion": "回顾"},
            {"title": "午后前瞻", "opportunities": []},
            {"title": "资金与情绪", "conclusion": "情绪"},
        ],
        ["LLM风险"],
    )
    report = m._build_midday_report(
        display, "raw", "brief", opportunities=["半导体"], risks=["光伏设备"]
    )
    afternoon = next(s for s in report["display_report"]["sections"] if s["title"] == "午后前瞻")
    assert afternoon["opportunities"] == ["半导体"]
    assert report["display_report"]["risks"] == ["光伏设备"]
    assert report["schema_version"] == "2.1"


def test_build_midday_report_backward_compatible_default():
    display = _display(
        [{"title": "午后前瞻", "opportunities": ["LLM机会"]}],
        ["LLM风险"],
    )
    report = m._build_midday_report(display, "raw", "brief")
    afternoon = next(s for s in report["display_report"]["sections"] if s["title"] == "午后前瞻")
    assert afternoon["opportunities"] == ["LLM机会"]
    assert report["display_report"]["risks"] == ["LLM风险"]


def test_build_midday_report_creates_afternoon_section_when_missing():
    display = _display([{"title": "上午盘面回顾", "conclusion": "回顾"}], [])
    report = m._build_midday_report(
        display, "raw", "brief", opportunities=["半导体"], risks=["光伏设备"]
    )
    assert any(
        s.get("title") == "午后前瞻" and s.get("opportunities") == ["半导体"]
        for s in report["display_report"]["sections"]
    )
