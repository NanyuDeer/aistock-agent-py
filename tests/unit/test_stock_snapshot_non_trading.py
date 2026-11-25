"""stock_snapshot 非交易时段 + 空数据降级单测。"""
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.stock_snapshot import stock_snapshot

SH = ZoneInfo("Asia/Shanghai")


async def _run_snapshot(quote_text: str, now: datetime) -> object:
    """构造 InsightGoal + mock get_quote + 注入时钟 → 调用 stock_snapshot。"""
    goal = InsightGoal(question="查 600519", symbols=["600519"], intent="stock_snapshot")
    with patch("aistock_agent.skills.stock_snapshot.get_quote") as mock_quote, \
         patch("aistock_agent.skills.stock_snapshot.datetime") as mock_dt, \
         patch("aistock_agent.skills.stock_snapshot.trading_session_status") as mock_status:
        mock_quote.ainvoke = AsyncMock(return_value=quote_text)
        mock_dt.now.return_value = now
        mock_status.return_value = (
            "pre_open" if now.astimezone(SH).time().hour < 9 else "trading",
            "今日开盘前（开盘时间 09:30）",
        )
        return await stock_snapshot({"symbol": "600519"}, goal)


@pytest.mark.asyncio
async def test_pre_open_with_empty_data_marks_degraded():
    """开盘前 + 数据为空 → degraded=True + facts 含"开盘前"提示。"""
    now = datetime(2026, 8, 3, 0, 53, tzinfo=ZoneInfo("UTC"))  # 北京时间 08:53
    ev = await _run_snapshot("未找到股票 600519 的行情数据", now)
    assert ev.degraded is True
    assert "数据源未返回" in (ev.degraded_reason or "")
    assert any("开盘前" in f for f in ev.facts)


@pytest.mark.asyncio
async def test_pre_open_with_valid_data_marks_degraded_with_hint():
    """开盘前 + 数据有效 → degraded=True + facts 含"最近交易日数据"提示。"""
    now = datetime(2026, 8, 3, 0, 53, tzinfo=ZoneInfo("UTC"))
    ev = await _run_snapshot("【贵州茅台】最新价: 1500  涨跌幅: 1.2%", now)
    assert ev.degraded is True
    assert "非交易时段" in (ev.degraded_reason or "")
    assert any("最近交易日数据" in f for f in ev.facts)


@pytest.mark.asyncio
async def test_trading_time_with_valid_data_not_degraded():
    """交易时段 + 数据有效 → degraded=False。"""
    now = datetime(2026, 8, 3, 2, 0, tzinfo=ZoneInfo("UTC"))  # 北京时间 10:00
    ev = await _run_snapshot("【贵州茅台】最新价: 1500  涨跌幅: 1.2%", now)
    assert ev.degraded is False
