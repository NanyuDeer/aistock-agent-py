"""完整洞察报告 PDF 渲染测试（2026-09-13）。"""

from aistock_agent.services.insight_report import build_report_sections, render_insight_report

_FULL_DATA = {
    "event": {
        "eventId": "mv:003018:2026-09-04:1788485647932:up",
        "symbol": "003018",
        "stockName": "金富科技",
        "triggeredAt": "2026-09-04T01:34:07.932Z",
        "direction": "up",
        "changePct": 7.19,
        "thresholdPct": 7,
        "severity": "medium",
        "latestPrice": 60.49,
        "previousClose": 56.43,
    },
    "attribution": {
        "primaryPhrase": "液冷服务器概念板块联动",
        "confidenceLevel": "medium",
        "candidates": [
            {"layer": "sector", "status": "supported", "verdict": "板块联动",
             "supportingEvidenceIds": ["e1"]},
        ],
        "chains": [
            {"role": "primary",
             "nodes": [{"stage": "trigger", "claim": "板块异动", "epistemicType": "fact",
                        "status": "established", "evidenceIds": ["e1"]}]},
        ],
        "unresolvedQuestions": ["资金持续性待观察"],
        "evidenceIndex": [
            {"source_id": "e1", "kind": "news", "title": "液冷概念走强",
             "content_excerpt": "板块涨 3%"},
        ],
    },
}


def test_build_sections_contains_all_chapters() -> None:
    sections = build_report_sections(_FULL_DATA)
    headings = [h for h, _ in sections]
    expected = ["事件事实", "主因结论", "五层候选归因", "六阶段因果链", "证据清单", "未解问题"]
    assert headings == expected


def test_missing_fields_render_as_placeholder() -> None:
    sections = build_report_sections({"event": {}, "attribution": {}})
    joined = "\n".join(line for _, lines in sections for line in lines)
    assert "暂缺" in joined


def test_render_returns_pdf_bytes() -> None:
    pdf = render_insight_report(build_report_sections(_FULL_DATA))
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000
