"""morning agent 降级内容检测单元测试

覆盖：
- _is_degraded_report：已知降级文本、schema 1.0 短内容、schema 2.0 空字段、正常报告
"""
from aistock_agent.agents.workers.morning import _is_degraded_report

# ── _is_degraded_report 测试 ─────────────────────────────────


def test_is_degraded_known_sorry_text():
    """包含 'Sorry, need more steps' → 降级。"""
    report = {
        "display_report": {
            "summary": "",
            "details": "Sorry, need more steps to process this request.",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    assert _is_degraded_report(report) is True


def test_is_degraded_schema_1_0_short_content():
    """schema 1.0 且 details < 100 字 → 降级。"""
    report = {
        "display_report": {
            "summary": "",
            "details": "短内容",
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    assert _is_degraded_report(report) is True


def test_is_degraded_schema_2_0_empty_stocks_and_risks():
    """schema 2.0 但 stocks 和 risks 都为空 → 降级。"""
    report = {
        "display_report": {
            "summary": "摘要",
            "details": "这是一段足够长的正常晨报内容，超过 100 字用于测试。" * 5,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }
    assert _is_degraded_report(report) is True


def test_is_degraded_normal_schema_2_0_report():
    """schema 2.0 且有 stocks → 正常。"""
    report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常晨报内容" * 30,
            "stocks": ["600519", "000001"],
            "risks": ["风险1"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }
    assert _is_degraded_report(report) is False


def test_is_degraded_normal_schema_1_0_long_content():
    """schema 1.0 但 details 足够长（>=100 字）→ 正常。"""
    report = {
        "display_report": {
            "summary": "",
            "details": "这是一段足够长的旧格式晨报内容，" * 20,
            "stocks": [],
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    assert _is_degraded_report(report) is False


def test_is_degraded_missing_display_report():
    """display_report 缺失 → 视为降级（容错）。"""
    report = {"podcast_brief": "", "schema_version": "1.0"}
    assert _is_degraded_report(report) is True


def test_is_degraded_schema_2_0_with_risks_only():
    """schema 2.0 且有 risks（stocks 空）→ 正常。"""
    report = {
        "display_report": {
            "summary": "摘要",
            "details": "正常晨报内容" * 30,
            "stocks": [],
            "risks": ["风险1", "风险2"],
        },
        "podcast_brief": "播报摘要",
        "schema_version": "2.0",
    }
    assert _is_degraded_report(report) is False
