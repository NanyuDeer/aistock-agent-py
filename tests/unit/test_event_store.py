import pytest
from unittest.mock import AsyncMock, patch

from aistock_agent.services.event_store import (
    EventRecord,
    event_content_hash,
    normalize_event,
    save_event_scrape,
    load_event_scrape,
)


def test_event_content_hash_is_stable_sha1():
    assert event_content_hash("A事件", "https://example.com/1") == event_content_hash("A事件", "https://example.com/1")
    assert event_content_hash("A事件", "https://example.com/1") != event_content_hash("B事件", "https://example.com/1")


def test_normalize_event_keeps_required_fields():
    raw = {
        "title": "央行降准",
        "summary": "央行宣布降准0.5个百分点",
        "url": "https://www.cls.cn/detail/123",
        "impact_score": 5,
        "direction": "positive",
        "involved_keywords": ["降准", "银行"],
    }
    event = normalize_event(raw, source="cls", score_date="2026-08-12")
    assert event is not None
    assert event["title"] == "央行降准"
    assert event["source"] == "cls"
    assert event["score_date"] == "2026-08-12"
    assert event["content_hash"]


def test_normalize_event_drops_missing_title():
    raw = {"summary": "没有标题", "url": "https://example.com/x"}
    assert normalize_event(raw, source="cls", score_date="2026-08-12") is None


def test_normalize_event_defaults_impact_score_low():
    raw = {"title": "普通公告", "url": "https://example.com/y"}
    event = normalize_event(raw, source="eastmoney", score_date="2026-08-12")
    assert event is not None
    assert event["impact_score"] == 0


@pytest.mark.asyncio
async def test_save_event_scrape_posts_to_analysis_reports():
    events = [
        {
            "event_id": "e1",
            "title": "事件A",
            "summary": "摘要A",
            "url": "https://example.com/1",
            "impact_score": 5,
            "direction": "positive",
            "involved_keywords": ["k"],
            "source": "cls",
            "source_level": "A",
            "content_hash": "abc",
            "scrape_at": "2026-08-12 10:00:00",
            "score_date": "2026-08-12",
            "payload": {},
        }
    ]
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        result = await save_event_scrape(events, "2026-08-12")
        assert result["persisted"] == 1
        mock_api.save_analysis_report.assert_awaited_once()
        call_kwargs = mock_api.save_analysis_report.call_args.kwargs
        assert call_kwargs["report_type"] == "event_scrape"
        assert call_kwargs["report_date"] == "2026-08-12"
        assert call_kwargs["content"]["events"][0]["title"] == "事件A"


@pytest.mark.asyncio
async def test_save_event_scrape_dedupes_same_batch():
    ev1 = {
        "event_id": "e1", "title": "事件A", "summary": "s", "url": "https://example.com/1",
        "impact_score": 5, "direction": "positive", "involved_keywords": [], "source": "cls",
        "source_level": "A", "content_hash": "abc", "scrape_at": "2026-08-12 10:00:00",
        "score_date": "2026-08-12", "payload": {},
    }
    ev2 = {**ev1, "event_id": "e2"}
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        result = await save_event_scrape([ev1, ev2], "2026-08-12")
        assert result["deduped"] == 1
        assert result["persisted"] == 1


@pytest.mark.asyncio
async def test_load_event_scrape_reads_by_date():
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report = AsyncMock(
            return_value={"content": {"events": [{"event_id": "e1", "title": "事件A"}]}}
        )
        events = await load_event_scrape("2026-08-12")
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"
        mock_api.get_analysis_report.assert_awaited_once_with("event_scrape", "2026-08-12")
