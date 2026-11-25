"""板块消息 → 关联个股（stock_trace 证据源）读取链路测试。

覆盖：
- `normalize_event` 写入 `sector_tags`
- `load_event_scrape` 对存量（无 sector_tags）事件按需回填
- `load_event_scrape_by_symbol`：不传 sectors 保持只按 symbol/keywords 匹配
- 传 sectors 时三态 `match_type`：symbol / sector / both
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_store import (
    load_event_scrape,
    load_event_scrape_by_symbol,
    normalize_event,
)


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "title": "半导体设备招标回暖",
        "summary": "国产替代加速",
        "url": "https://example.com/a",
        "source_level": "C",
        "involved_keywords": [],
    }
    raw.update(overrides)
    return raw


def _stored_event(**overrides: Any) -> dict[str, Any]:
    """事件库 content.events 中的存量事件形状（逐字段构造用最小集）。"""
    ev: dict[str, Any] = {
        "event_id": "2026-08-12-abcdef0123456789",
        "title": "半导体设备招标回暖",
        "summary": "国产替代加速",
        "url": "https://example.com/a",
        "impact_score": 3,
        "direction": "positive",
        "involved_keywords": [],
        "source": "cls",
        "source_level": "C",
        "content_hash": "hash",
        "scrape_at": "2026-08-12 09:00:00",
        "score_date": "2026-08-12",
        "payload": {},
    }
    ev.update(overrides)
    return ev


def _report(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"content": {"events": events}}


async def _load_with(events: list[dict[str, Any]]) -> Any:
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(return_value=_report(events))
        return await load_event_scrape("2026-08-12")


@pytest.mark.asyncio
async def test_normalize_event_fills_sector_tags() -> None:
    event = normalize_event(_raw(), source="cls", score_date="2026-08-12")
    assert event is not None
    assert "半导体" in event["sector_tags"]


@pytest.mark.asyncio
async def test_load_event_scrape_backfills_missing_sector_tags() -> None:
    """存量事件没有 sector_tags 字段 → 读时现场计算，无需回填迁移。"""
    events = await _load_with([_stored_event()])
    assert len(events) == 1
    assert "半导体" in events[0]["sector_tags"]


@pytest.mark.asyncio
async def test_load_event_scrape_keeps_stored_sector_tags() -> None:
    events = await _load_with([_stored_event(sector_tags=["光伏"])])
    assert events[0]["sector_tags"] == ["光伏"]


@pytest.mark.asyncio
async def test_load_by_symbol_without_sectors_is_unchanged() -> None:
    """不传 sectors：只按 payload.symbol / involved_keywords 匹配（行为不变）。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report(
                [
                    _stored_event(event_id="e1", payload={"symbol": "600000"}),
                    _stored_event(event_id="e2", title="半导体板块走强", payload={}),
                ]
            )
        )
        events = await load_event_scrape_by_symbol("600000", "2026-08-12")
    assert [ev["event_id"] for ev in events] == ["e1"]
    assert events[0].get("match_type") == "symbol"


@pytest.mark.asyncio
async def test_load_by_symbol_with_sectors_returns_three_match_types() -> None:
    """funds: symbol 命中 / 仅板块命中 / 两者都命中 / 都不命中。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report(
                [
                    _stored_event(
                        event_id="symbol-only",
                        title="公司发布回购公告",
                        payload={"symbol": "600000"},
                    ),
                    _stored_event(event_id="sector-only", title="半导体设备招标回暖", payload={}),
                    _stored_event(
                        event_id="both",
                        title="半导体设备招标回暖",
                        payload={"symbol": "600000"},
                    ),
                    _stored_event(event_id="nothing", title="白酒动销回暖", payload={}),
                ]
            )
        )
        events = await load_event_scrape_by_symbol(
            "600000", "2026-08-12", sectors=["半导体"]
        )

    by_id = {ev["event_id"]: ev.get("match_type") for ev in events}
    assert by_id == {
        "symbol-only": "symbol",
        "sector-only": "sector",
        "both": "both",
    }


@pytest.mark.asyncio
async def test_load_by_symbol_sector_match_supports_literal_board_name() -> None:
    """板块名未被别名词典收录时，退回「板块名字面出现在标题/摘要」的匹配。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report(
                [_stored_event(event_id="e1", title="量子计算再获政策支持", payload={})]
            )
        )
        events = await load_event_scrape_by_symbol(
            "600000", "2026-08-12", sectors=["量子计算"]
        )
    assert [ev["event_id"] for ev in events] == ["e1"]


@pytest.mark.asyncio
async def test_broad_index_sector_never_recalls_events() -> None:
    """宽基指数/两融/互联互通板块不参与召回（即使事件标题里出现板块名）。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report(
                [_stored_event(event_id="e1", title="上证180成份股名单调整", payload={})]
            )
        )
        events = await load_event_scrape_by_symbol(
            "600000", "2026-08-12", sectors=["上证180成份股", "融资融券", "沪股通"]
        )
    assert events == []


@pytest.mark.asyncio
async def test_cross_concept_alias_does_not_recall_umbrella_news() -> None:
    """DeepSeek概念 不扩张到 AI 伞形标准名 → AI 板块新闻不再被误召回。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report(
                [_stored_event(event_id="e1", title="AI芯片出货量大增", payload={})]
            )
        )
        events = await load_event_scrape_by_symbol(
            "600000", "2026-08-12", sectors=["DeepSeek概念"]
        )
    assert events == []


@pytest.mark.asyncio
async def test_cross_concept_alias_still_matches_literal_event() -> None:
    """收紧扩张后，标题字面提到「DeepSeek概念」的事件仍应被召回。"""
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value=_report(
                [_stored_event(event_id="e1", title="DeepSeek概念板块活跃", payload={})]
            )
        )
        events = await load_event_scrape_by_symbol(
            "600000", "2026-08-12", sectors=["DeepSeek概念"]
        )
    assert [ev["event_id"] for ev in events] == ["e1"]
    assert events[0].get("match_type") == "sector"
