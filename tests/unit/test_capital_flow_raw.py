"""capital_flow skill raw.flow 结构化字段单测（P11 线 3，spec §3.1/§3.4）。"""
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.capital_flow import capital_flow

SH = ZoneInfo("Asia/Shanghai")

# /internal/flow/:symbol 新浪源字段（数值型，Number||0 保证）
FLOW_OK: dict[str, object] = {
    "r0_in": 1_000_000.0,
    "r0_out": 600_000.0,
    "netamount": 400_000.0,
    "name": "贵州茅台",
    "trade": 1500.0,
}


def _goal() -> InsightGoal:
    return InsightGoal(question="查 600519 资金", symbols=["600519"], intent="stock_snapshot")


async def _run(flow_text: str, flow_data: dict | None, now: datetime,
               status: tuple[str, str] = ("trading", "盘中")) -> object:
    with (
        patch("aistock_agent.skills.capital_flow.get_capital_flow") as mock_flow,
        patch("aistock_agent.skills.capital_flow.node_api") as mock_api,
        patch("aistock_agent.skills.capital_flow.datetime") as mock_dt,
        patch("aistock_agent.skills.capital_flow.trading_session_status") as mock_status,
    ):
        mock_flow.ainvoke = AsyncMock(return_value=flow_text)
        mock_api.get = AsyncMock(return_value=flow_data)
        mock_dt.now.return_value = now
        mock_status.return_value = status
        return await capital_flow({"symbol": "600519"}, _goal())


@pytest.mark.asyncio
async def test_raw_flow_mapped_from_sina_fields():
    """新浪字段 → raw.flow 英文键映射（flow_5d 恒空数组）。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ev = await _run("主力流入: 1000000  主力流出: 600000\n主力净流入: 400000", FLOW_OK, now)
    assert ev.raw["flow"] == {
        "main_in": 1_000_000.0,
        "main_out": 600_000.0,
        "net_amount": 400_000.0,
        "flow_5d": [],
    }


@pytest.mark.asyncio
async def test_raw_flow_none_when_api_returns_none():
    """node_api.get 返回 None → raw.flow 为 None（不阻塞对话）。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ev = await _run("未找到股票 600519 的资金流向数据", None, now)
    assert ev.raw["flow"] is None
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_raw_flow_partial_fields_omitted():
    """Tushare 兜底无 r0_* 键 → 仅保留存在的字段 + flow_5d 空数组。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ev = await _run("主力流入: 1", {"r0_in": 1.0}, now)
    assert ev.raw["flow"] == {"main_in": 1.0, "flow_5d": []}


@pytest.mark.asyncio
async def test_raw_flow_present_when_degraded_non_trading():
    """非交易时段 degraded 时仍产出 flow（数据来自最近交易日）。"""
    now = datetime(2026, 8, 5, 8, 53, tzinfo=ZoneInfo("Asia/Shanghai"))  # 北京时间 08:53
    ev = await _run("主力流入: 1000000", FLOW_OK, now,
                    ("pre_open", "今日开盘前（开盘时间 09:30）"))
    assert ev.degraded is True
    assert "非交易时段" in (ev.degraded_reason or "")
    assert ev.raw["flow"]["net_amount"] == 400_000.0
