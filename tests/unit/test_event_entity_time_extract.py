"""事件时间抽取 + 物化挂接测试（spec §5B.3 / §10.2，P0.5）。

- `_extract_event_start_time`：只认明确绝对日期 / 日期区间（区间取开始日）；
  无明确日期 → None（禁 LLM 猜日期，spec §5B.3）。
- `_materialize_event_entity`：开关开启且未来事件才 POST /internal/event-entities；
  端点未落地/失败 → warning 跳过，不抛异常、不阻断主链路。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.services.event_scrape_sources import (
    _extract_event_start_time,
    _materialize_event_entity,
)
from aistock_agent.services.event_store import EventRecord


def _rec(title: str, summary: str = "", score_date: str = "2026-09-15") -> EventRecord:
    return EventRecord(
        event_id=f"{score_date}-abcdef1234567890",
        title=title,
        summary=summary,
        url="",
        impact_score=5,
        direction="unknown",
        involved_keywords=[],
        source="calendar",
        source_level="A",
        content_hash="abcdef1234567890",
        scrape_at=f"{score_date} 08:00:00",
        score_date=score_date,
        payload={},
        symbol="",
        stock_name="",
        industry="",
        event_scope="UNKNOWN",
        event_scope_source="rule",
        event_scope_confidence=0.0,
        app_event_id=None,
    )


# ── _extract_event_start_time（纯函数）──


def test_extract_absolute_date_only():
    # 明确绝对日期（无年份 M/D，ref_year 兜底）→ 返回（time_source=news_extraction 语义）
    assert (
        _extract_event_start_time("华为将于 9/23 发布新品", "详见正文", "2026-09-15")
        == "2026-09-23"
    )


def test_extract_date_range_takes_start():
    # 区间「9/23 至 9/25」→ 取开始日 9/23（多日事件只在开始日落点，spec §1.5）
    assert (
        _extract_event_start_time(
            "第二十七届光电博览会 9/23 至 9/25 深圳", "9/23-9/25", "2026-09-15"
        )
        == "2026-09-23"
    )


def test_extract_no_date_returns_none():
    # 无明确绝对日期 → None（禁 LLM 猜日期，spec §5B.3）
    assert (
        _extract_event_start_time("某公司回应关税影响", "公司股价大跌", "2026-09-15")
        is None
    )


def test_extract_year_prefixed_absolute_date():
    # 含年份形式绝对日期优先（2026-09-17），不因无年份 M/D 兜底而误判
    assert (
        _extract_event_start_time("议息会议定于 2026-09-17 举行", "", "2026-09-15")
        == "2026-09-17"
    )


# ── _materialize_event_entity（开关内物化）──


@pytest.mark.asyncio
async def test_materialize_future_event_posts(monkeypatch):
    monkeypatch.setattr(settings, "event_entity_enabled", True)
    with patch(
        "aistock_agent.services.event_scrape_sources.node_api.post_event_entity",
        new=AsyncMock(return_value={"event_id": "EVT-0001"}),
    ) as m:
        await _materialize_event_entity(_rec("华为将于 9/23 发布新品"), "2026-09-15T09:00:00+08:00")
    body = m.call_args.args[0]
    assert body["event_start_time"] == "2026-09-23T00:00:00+08:00"
    assert body["time_source"] == "news_extraction"
    assert body["source_type"] == "news"


@pytest.mark.asyncio
async def test_materialize_skips_when_disabled(monkeypatch):
    # 开关默认 False（app-api 端点未落地）：短路、不发起 HTTP
    monkeypatch.setattr(settings, "event_entity_enabled", False)
    with patch(
        "aistock_agent.services.event_scrape_sources.node_api.post_event_entity",
        new=AsyncMock(),
    ) as m:
        await _materialize_event_entity(_rec("华为将于 9/23 发布新品"), "2026-09-15T09:00:00+08:00")
    m.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_skips_non_future_or_undated_event(monkeypatch):
    """无明确未来日期 → 不物化（已发生物化对账留收口清单，spec §5B.3）。"""
    monkeypatch.setattr(settings, "event_entity_enabled", True)
    with patch(
        "aistock_agent.services.event_scrape_sources.node_api.post_event_entity",
        new=AsyncMock(),
    ) as m:
        await _materialize_event_entity(
            _rec("某公司回应关税影响", "公司股价大跌"), "2026-09-15T09:00:00+08:00"
        )
    m.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_occurred_event_posts(monkeypatch):
    """已发生事件（有明确绝对日期）同样物化（spec §5B.3 第 4 条），信度 0.9。"""
    monkeypatch.setattr(settings, "event_entity_enabled", True)
    with patch(
        "aistock_agent.services.event_scrape_sources.node_api.post_event_entity",
        new=AsyncMock(return_value={"event_id": "EVT-0001", "event_status": "occurred"}),
    ) as m:
        result = await _materialize_event_entity(
            _rec("9/1 已发生的发布会", score_date="2026-09-01"),
            "2026-09-15T09:00:00+08:00",
        )
    body = m.call_args.args[0]
    assert body["event_start_time"] == "2026-09-01T00:00:00+08:00"
    assert body["time_confidence"] == 0.9
    # 返回值含 event_id/event_status（供外层回填）
    assert result == {"event_id": "EVT-0001", "event_status": "occurred"}


@pytest.mark.asyncio
async def test_materialize_returns_none_without_date(monkeypatch):
    """抽不出日期（publish_time_fallback 语义）→ 不物化、返回 None（不 SUP 注入）。"""
    monkeypatch.setattr(settings, "event_entity_enabled", True)
    with patch(
        "aistock_agent.services.event_scrape_sources.node_api.post_event_entity",
        new=AsyncMock(),
    ) as m:
        result = await _materialize_event_entity(
            _rec("某公司回应关税影响", "公司股价大跌"),
            "2026-09-15T09:00:00+08:00",
        )
    m.assert_not_called()
    assert result is None


@pytest.mark.asyncio
async def test_materialize_failure_does_not_raise(monkeypatch):
    """端点未落地/失败 → warning 跳过，不抛异常（降级纪律）。"""
    monkeypatch.setattr(settings, "event_entity_enabled", True)
    with patch(
        "aistock_agent.services.event_scrape_sources.node_api.post_event_entity",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await _materialize_event_entity(_rec("华为将于 9/23 发布新品"), "2026-09-15T09:00:00+08:00")
