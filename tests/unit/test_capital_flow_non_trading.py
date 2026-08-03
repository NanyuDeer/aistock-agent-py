"""capital_flow 非交易时段 + 空数据降级单测。"""
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.capital_flow import capital_flow

SH = ZoneInfo("Asia/Shanghai")


async def _run_capital(flow_text: str, now: datetime) -> object:
    goal = InsightGoal(question="查 600519 资金", symbols=["600519"], intent="stock_snapshot")
    with patch("aistock_agent.skills.capital_flow.get_capital_flow") as mock_flow, \
         patch("aistock_agent.skills.capital_flow.datetime") as mock_dt, \
         patch("aistock_agent.skills.capital_flow.trading_session_status") as mock_status:
        mock_flow.ainvoke = AsyncMock(return_value=flow_text)
        mock_dt.now.return_value = now
        mock_status.return_value = ("pre_open", "今日开盘前（开盘时间 09:30）")
        return await capital_flow({"symbol": "600519"}, goal)


@pytest.mark.asyncio
async def test_pre_open_with_empty_data_marks_degraded():
    now = datetime(2026, 8, 3, 0, 53, tzinfo=ZoneInfo("UTC"))
    ev = await _run_capital("未找到股票 600519 的资金流向数据", now)
    assert ev.degraded is True
    assert any("开盘前" in f for f in ev.facts)
