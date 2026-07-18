from pathlib import Path

from aistock_agent.agents.workers import review

REVIEW_MARKDOWN = """# 收盘复盘

## 步骤4：输出核心结论
市场风险偏好改善，科技板块领涨。

## 步骤5：标准化行情事实附录
<!--SECTOR_LIST_START-->
- 半导体
- AI算力
<!--SECTOR_LIST_END-->
"""


def test_build_review_report_uses_schema_v2_and_keeps_markdown():
    report = review._build_review_report(REVIEW_MARKDOWN)
    assert report == {
        "display_report": {
            "summary": "市场风险偏好改善，科技板块领涨。",
            "details": REVIEW_MARKDOWN,
            "stocks": [],
            "sectors": ["半导体", "AI算力"],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "2.0",
    }


def test_build_review_report_falls_back_to_appendix_b_fixture():
    markdown = Path("tests/fixtures/sample_review_report.md").read_text(encoding="utf-8")
    report = review._build_review_report(markdown)
    assert report["display_report"]["summary"] == ""
    assert report["display_report"]["sectors"] == ["黄金", "贵金属", "半导体", "新能源车"]
