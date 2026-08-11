"""tests/unit/test_iterate_case_scanner.py"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.case_scanner import find_recent_trading_day, scan_major_events


@pytest.mark.asyncio
async def test_find_recent_trading_day_uses_close_snapshot() -> None:
    with patch(
        "aistock_agent.iterate.case_scanner.node_api.get",
        AsyncMock(return_value={"trade_date": "2026-07-31", "status": "complete"}),
    ) as mock_get:
        day = await find_recent_trading_day()
    assert day == "2026-07-31"
    mock_get.assert_awaited_once_with("/internal/market/close-snapshot")


@pytest.mark.asyncio
async def test_find_recent_trading_day_falls_back_to_last_close() -> None:
    async def fake_get(path: str) -> dict[str, object] | None:
        if path == "/internal/market/close-snapshot":
            return None
        return {"trade_date": "20260730"}

    with patch("aistock_agent.iterate.case_scanner.node_api.get", side_effect=fake_get):
        day = await find_recent_trading_day()
    assert day == "2026-07-30"  # 归一化 YYYYMMDD -> YYYY-MM-DD


@pytest.mark.asyncio
async def test_find_recent_trading_day_none_when_all_fail() -> None:
    with patch(
        "aistock_agent.iterate.case_scanner.node_api.get",
        AsyncMock(return_value=None),
    ):
        assert await find_recent_trading_day() is None


@pytest.mark.asyncio
async def test_scan_major_events_clusters_by_keyword_and_window() -> None:
    """事件聚类：关键词命中 + 30 分钟窗口合并 + telegraph_records 取 [锚点, T] 窗口全部电报。"""
    records = [
        {
            "time": "2026-07-31T09:00:00+08:00",
            "title": "隔夜美股暴涨",
            "content": "纳指涨2.5%",
            "url": "u1",
        },
        {
            "time": "2026-07-31T09:10:00+08:00",
            "title": "美股三大指数集体收涨",
            "content": "标普涨1.8%",
            "url": "u2",
        },
        {
            "time": "2026-07-31T10:00:00+08:00",
            "title": "A股高开高走",
            "content": "沪指涨1%",
            "url": "u3",
        },
        {
            "time": "2026-07-31T11:00:00+08:00",
            "title": "央行开展逆回购",
            "content": "5000亿",
            "url": "u4",
        },
    ]
    with patch(
        "aistock_agent.iterate.case_scanner.node_api.get_list",
        AsyncMock(return_value=records),
    ):
        events = await scan_major_events(days=1)

    # 09:00 命中关键词 + 09:10 未命中但落在 [锚点, T] 窗口 → 合并为一个事件；
    # 10:00 未命中关键词且超出窗口（09:10 之后）→ 不进任何事件；11:00 央行命中 → 独立事件
    assert len(events) == 2
    first = events[0]
    assert first["event_time"] == "2026-07-31T09:10:00+08:00"  # T = 末条电报时间
    assert len(first["telegraph_records"]) == 2  # [锚点, T] 窗口内所有电报（含 09:10 未命中的报道）
    assert "暴涨" in str(first["event_title"])
