"""Tests for event_persister — verify request body structure for event_conduction reports.

Asserts that the Node API request body contains:
- report_type="event_conduction"
- event_id (not fixed user_id="system")
- complete analysis_reports (four modules + podcast) — deep
  equality, not just key existence
- event_meta fields (eventId, title, source) correctly mapped into content
- original event text preserved

Additional:
- Verifies event_id consistency: argument event_id == body.event_id == content.eventId
- Verifies analysis_reports is the exact same dict passed in (data integrity)
- Verifies event_meta values are extracted into content (not hardcoded)
- Verifies silent failure degradation
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_persister import persist_event_report


def _make_analysis_reports() -> dict[str, object]:
    """Build a complete analysis_reports dict with all four modules + podcast."""
    return {
        "event_understanding": {"summary": "Test understanding summary"},
        "event_transmission": {"mechanism": "Test transmission mechanism"},
        "event_history": [{"date": "2024-01-01", "event": "Historical event"}],
        "event_investment": {"conclusion": "Test investment conclusion"},
        "event_podcast_brief": "Test podcast brief text",
    }


def _make_event_meta() -> dict[str, object]:
    """Build event metadata dict."""
    return {
        "eventId": "evt_test12345",
        "title": "Test Event Title",
        "source": "cls",
    }


@pytest.mark.asyncio
async def test_persist_event_report_request_body():
    """Verify the request body sent to Node API contains:
    - report_type="event_conduction"
    - event_id (consistent across body.event_id and content.eventId)
    - complete analysis_reports (deep equality with the argument, not just key existence)
    - event_meta fields (title, source) correctly mapped into content
    - no fixed user_id="system"
    - original event text preserved
    """
    event_id = "evt_test12345"
    event_meta = _make_event_meta()
    event_text = "某公司发布重大利好公告，预计对产业链产生深远影响。"
    analysis_reports = _make_analysis_reports()

    with patch(
        "aistock_agent.services.event_persister.node_api.post",
        new_callable=AsyncMock,
    ) as mock_post:
        await persist_event_report(event_id, event_meta, event_text, analysis_reports)

        # Verify node_api.post was called exactly once
        mock_post.assert_called_once()

        # Extract the request body (positional args: url, body)
        call_args = mock_post.call_args
        url = call_args.args[0]
        body = call_args.args[1]

        # Assert URL
        assert url == "/internal/analysis-reports"

        # Assert report_type
        assert body["report_type"] == "event_conduction"

        # Assert event_id is present in the request body and matches the argument
        assert body["event_id"] == event_id

        # Assert no fixed user_id="system"
        assert body.get("user_id") != "system"

        # ── Assert content structure ──
        content = body["content"]

        # event_id consistency: body.event_id == content.eventId == event_meta["eventId"]
        assert content["eventId"] == event_id
        assert content["eventId"] == event_meta["eventId"]

        # event_meta fields are correctly mapped into content (not hardcoded)
        assert content["title"] == event_meta["title"]
        assert content["source"] == event_meta["source"]

        # Original event text is preserved in content
        assert content["event"] == event_text

        # ── Assert analysis_reports deep equality ──
        # The exact dict passed as argument must be the one in the request body.
        # This verifies data integrity — not just that keys exist, but that values match.
        ar = content["analysis_reports"]
        assert ar == analysis_reports, (
            "analysis_reports in request body must be deeply equal to the argument passed"
        )

        # Explicitly verify all four modules + podcast are present with correct values
        assert ar["event_understanding"] == analysis_reports["event_understanding"]
        assert ar["event_transmission"] == analysis_reports["event_transmission"]
        assert ar["event_history"] == analysis_reports["event_history"]
        assert ar["event_investment"] == analysis_reports["event_investment"]
        assert ar["event_podcast_brief"] == analysis_reports["event_podcast_brief"]


@pytest.mark.asyncio
async def test_persist_event_report_event_meta_not_hardcoded():
    """Verify that event_meta values are passed through, not hardcoded.

    Uses different event_meta values to ensure the persister doesn't
    have hardcoded title/source/eventId strings.
    """
    event_id = "evt_different_999"
    event_meta = {
        "eventId": "evt_different_999",
        "title": "A completely different event title",
        "source": "sina",
    }
    event_text = "不同的事件原文"
    analysis_reports = _make_analysis_reports()

    with patch(
        "aistock_agent.services.event_persister.node_api.post",
        new_callable=AsyncMock,
    ) as mock_post:
        await persist_event_report(event_id, event_meta, event_text, analysis_reports)

        call_args = mock_post.call_args
        body = call_args.args[1]

        # event_id from argument matches body and content
        assert body["event_id"] == "evt_different_999"
        assert body["content"]["eventId"] == "evt_different_999"

        # event_meta values are passed through (not hardcoded)
        assert body["content"]["title"] == "A completely different event title"
        assert body["content"]["source"] == "sina"

        # event_text is passed through
        assert body["content"]["event"] == "不同的事件原文"

        # analysis_reports deep equality
        assert body["content"]["analysis_reports"] == analysis_reports


@pytest.mark.asyncio
async def test_persist_event_report_silent_failure():
    """Verify that persistence failure is silently swallowed (no exception raised).

    The event_persister must not raise — it's a non-critical path.
    This ensures the event Agent main flow is not affected by persistence failures.
    """
    event_id = "evt_fail"
    event_meta = {"eventId": "evt_fail", "title": "Fail Test", "source": ""}
    event_text = "test event"
    analysis_reports = _make_analysis_reports()

    with patch(
        "aistock_agent.services.event_persister.node_api.post",
        new_callable=AsyncMock,
        side_effect=Exception("Connection refused"),
    ):
        # Should not raise — silent degradation
        await persist_event_report(event_id, event_meta, event_text, analysis_reports)


@pytest.mark.asyncio
async def test_persist_event_report_includes_source_name_and_event_type():
    """source_name/event_type 从 event_meta 落入 content JSONB 顶层（无需改表结构）。"""
    event_id = "evt_meta_ext"
    event_meta = {
        "eventId": "evt_meta_ext",
        "title": "测试事件",
        "source": "https://m.sohu.com/a/123",
        "source_name": "搜狐",
        "event_type": "市场动态",
    }
    event_text = "事件原文"
    analysis_reports = _make_analysis_reports()

    with patch(
        "aistock_agent.services.event_persister.node_api.post",
        new_callable=AsyncMock,
    ) as mock_post:
        await persist_event_report(event_id, event_meta, event_text, analysis_reports)

        body = mock_post.call_args.args[1]
        content = body["content"]
        # 新字段写入 content 顶层；旧字段保持
        assert content["source_name"] == "搜狐"
        assert content["event_type"] == "市场动态"
        assert content["source"] == "https://m.sohu.com/a/123"
        assert content["eventId"] == "evt_meta_ext"
        assert content["analysis_reports"] == analysis_reports
