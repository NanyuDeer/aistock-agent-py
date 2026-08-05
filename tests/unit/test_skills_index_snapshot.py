"""index_snapshot skill 单测（P5 工作线 B）"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.index_snapshot import DEFAULT_SYMBOLS, index_snapshot


def _indices() -> list[dict]:
    return [
        {
            "index": "000001", "name": "上证指数",
            "price": 3832.26, "changePercent": 0.72, "changeAmount": 27.0,
        },
        {
            "index": "399001", "name": "深证成指",
            "price": 12000.0, "changePercent": -0.5, "changeAmount": -60.0,
        },
    ]


def _goal() -> InsightGoal:
    return InsightGoal(question="沪指今天怎么样", intent="index_snapshot")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_index_snapshot_basic():
    client = AsyncMock()
    client.get = AsyncMock(return_value={"indices": _indices()})
    with patch("aistock_agent.skills.index_snapshot.node_api", client):
        ev: Evidence = await index_snapshot({}, _goal())
    assert ev.skill_name == "index_snapshot"
    assert not ev.degraded
    assert any("上证指数" in f for f in ev.facts)
    assert len(ev.sources) == 2
    assert ev.raw["source"] == "index_quotes"


@pytest.mark.asyncio
async def test_index_snapshot_partial_null_not_degraded():
    client = AsyncMock()
    client.get = AsyncMock(return_value={
        "indices": [
            {
                "index": "000001", "name": "上证指数",
                "price": None, "changePercent": None, "changeAmount": None,
            },
            {
                "index": "399001", "name": "深证成指",
                "price": 12000.0, "changePercent": -0.5, "changeAmount": -60.0,
            },
        ]
    })
    with patch("aistock_agent.skills.index_snapshot.node_api", client):
        ev = await index_snapshot({}, _goal())
    assert ev.degraded is False  # 部分为 null 不算整体 degraded


@pytest.mark.asyncio
async def test_index_snapshot_all_failed_degraded():
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    with patch("aistock_agent.skills.index_snapshot.node_api", client):
        ev = await index_snapshot({}, _goal())
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_index_snapshot_default_symbols():
    client = AsyncMock()
    client.get = AsyncMock(return_value={"indices": []})
    with patch("aistock_agent.skills.index_snapshot.node_api", client):
        await index_snapshot({}, _goal())
    client.get.assert_awaited_once_with(
        "/internal/index/quotes?symbols=" + ",".join(DEFAULT_SYMBOLS)
    )
