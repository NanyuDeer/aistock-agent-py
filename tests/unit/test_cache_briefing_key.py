"""services/cache.py 缓存 key 按 report_type 参数化测试（H2，2026-08-24）

覆盖：
- get/set_cached_briefing 签名含 report_type 参数且默认 "morning"
- 缓存 key 按 report_type 拼接：briefing:{report_type}:{YYYY-MM-DD}（防盘中报撞键）
- 默认 report_type="morning" 时保持现有调用行为（regression-free）
"""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import cache


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def test_get_cached_briefing_signature_has_report_type():
    """get_cached_briefing 签名含 report_type，默认 "morning"。"""
    import inspect

    params = inspect.signature(cache.get_cached_briefing).parameters
    assert "report_type" in params
    assert params["report_type"].default == "morning"


@pytest.mark.asyncio
async def test_get_cached_briefing_key_uses_report_type():
    """报告类型为 midday 时，get 查询 briefing:midday 键。"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=None)
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.get_cached_briefing(report_type="midday")
    mock_client.get.assert_awaited_once_with(f"briefing:midday:{_today()}")


@pytest.mark.asyncio
async def test_get_cached_briefing_key_defaults_morning():
    """默认（无参）调用 get 查询 briefing:morning 键（regression-free）。"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=None)
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.get_cached_briefing()
    mock_client.get.assert_awaited_once_with(f"briefing:morning:{_today()}")


def test_set_cached_briefing_signature_has_report_type():
    """set_cached_briefing 签名含 report_type，默认 "morning"。"""
    import inspect

    params = inspect.signature(cache.set_cached_briefing).parameters
    assert "report_type" in params
    assert params["report_type"].default == "morning"


@pytest.mark.asyncio
async def test_set_cached_briefing_key_uses_report_type():
    """报告类型为 midday 时，setex 写入 briefing:midday 键。"""
    mock_client = AsyncMock()
    mock_client.setex = AsyncMock()
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.set_cached_briefing("content", report_type="midday")
    mock_client.setex.assert_awaited_once_with(
        f"briefing:midday:{_today()}", 86400, "content"
    )


@pytest.mark.asyncio
async def test_set_cached_briefing_key_defaults_morning():
    """默认（无参）调用 setex 写入 briefing:morning 键（regression-free）。"""
    mock_client = AsyncMock()
    mock_client.setex = AsyncMock()
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.set_cached_briefing("content")
    mock_client.setex.assert_awaited_once_with(
        f"briefing:morning:{_today()}", 86400, "content"
    )