"""stock_history skill 单测（P5 D41）"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.stock_history import stock_history


def _rows() -> list[dict]:
    return [
        {"trade_date": "2026-08-01", "open": 100.0, "high": 110.0,
         "low": 99.0, "close": 108.0, "pct_chg": 2.0},
        {"trade_date": "2026-07-31", "open": 99.0, "high": 102.0,
         "low": 98.0, "close": 100.0, "pct_chg": -1.0},
        {"trade_date": "2026-07-30", "open": 100.0, "high": 101.0,
         "low": 97.0, "close": 99.0, "pct_chg": 0.5},
    ]


def _goal() -> InsightGoal:
    return InsightGoal(question="走势", intent="stock_history", symbols=["600519"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_history_basic():
    client = AsyncMock()
    client.get = AsyncMock(
        return_value={"symbol": "600519", "klt": 101, "days": 3, "rows": _rows()}
    )
    with patch("aistock_agent.skills.stock_history.node_api", client):
        ev: Evidence = await stock_history({"symbol": "600519", "days": 30}, _goal())
    assert ev.skill_name == "stock_history"
    assert not ev.degraded
    assert ev.raw["days"] == 3
    assert any("区间" in f or "108.0" in f for f in ev.facts)


@pytest.mark.asyncio
async def test_history_empty_rows_degraded():
    client = AsyncMock()
    client.get = AsyncMock(return_value={"symbol": "600519", "klt": 101, "days": 0, "rows": []})
    with patch("aistock_agent.skills.stock_history.node_api", client):
        ev = await stock_history({"symbol": "600519", "days": 30}, _goal())
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_history_node_failure_degraded():
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    with patch("aistock_agent.skills.stock_history.node_api", client):
        ev = await stock_history({"symbol": "600519"}, _goal())
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_history_missing_symbol_raises():
    # 裸函数（@skill 装饰后异常→degraded Evidence，测试原函数守卫）
    # args 与 goal.symbols 均无 symbol 才触发 ValueError（goal.symbols 可兜底）
    goal = InsightGoal(question="走势", intent="stock_history", symbols=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await stock_history.__wrapped__({}, goal)
