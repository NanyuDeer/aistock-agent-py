"""trend_ranking skill 单测（P5 D42）"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.trend_ranking import MAX_LIMIT, trend_ranking


def _items() -> list[dict]:
    return [
        {"symbol": "600519", "name": "贵州茅台", "industry": "白酒", "score": 88.5, "label": "S"},
        {"symbol": "000858", "name": "五粮液", "industry": "白酒", "score": 82.0, "label": "A"},
    ]


def _goal() -> InsightGoal:
    return InsightGoal(question="排名", intent="trend_ranking")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ranking_basic():
    client = AsyncMock()
    client.get_list = AsyncMock(return_value=_items())
    with patch("aistock_agent.skills.trend_ranking.node_api", client):
        ev: Evidence = await trend_ranking({"limit": 10}, _goal())
    assert ev.skill_name == "trend_ranking"
    assert not ev.degraded
    assert len(ev.facts) == len(_items())
    assert any("贵州茅台" in f for f in ev.facts)
    assert ev.raw["limit"] == 10


@pytest.mark.asyncio
async def test_ranking_empty_degraded():
    client = AsyncMock()
    client.get_list = AsyncMock(return_value=[])
    with patch("aistock_agent.skills.trend_ranking.node_api", client):
        ev = await trend_ranking({}, _goal())
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_ranking_node_failure_degraded():
    client = AsyncMock()
    client.get_list = AsyncMock(return_value=None)
    with patch("aistock_agent.skills.trend_ranking.node_api", client):
        ev = await trend_ranking({}, _goal())
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_ranking_limit_capped():
    client = AsyncMock()
    client.get_list = AsyncMock(return_value=_items())
    with patch("aistock_agent.skills.trend_ranking.node_api", client):
        await trend_ranking({"limit": 999}, _goal())
    client.get_list.assert_awaited_once_with(f"/internal/trend/top?limit={MAX_LIMIT}")
