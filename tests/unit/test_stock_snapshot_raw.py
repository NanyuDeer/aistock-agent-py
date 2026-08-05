"""stock_snapshot skill raw.quote 结构化字段单测（P11 线 3，spec §3.1/§3.4）。"""
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.stock_snapshot import stock_snapshot

SH = ZoneInfo("Asia/Shanghai")

# /internal/quote/:symbol 实际返回 core 级中文键 dict（TencentQuoteService core level）
QUOTE_OK: dict[str, object] = {
    "股票代码": "600519",
    "股票简称": "贵州茅台",
    "最新价": 1500.0,
    "涨跌幅": 1.2,
    "行情时间": "20260805100000",
}


def _goal() -> InsightGoal:
    return InsightGoal(question="查 600519", symbols=["600519"], intent="stock_snapshot")


async def _run(quote_text: str, quote_data: dict | None, now: datetime,
               status: tuple[str, str] = ("trading", "盘中")) -> object:
    with (
        patch("aistock_agent.skills.stock_snapshot.get_quote") as mock_quote,
        patch("aistock_agent.skills.stock_snapshot.node_api") as mock_api,
        patch("aistock_agent.skills.stock_snapshot.datetime") as mock_dt,
        patch("aistock_agent.skills.stock_snapshot.trading_session_status") as mock_status,
    ):
        mock_quote.ainvoke = AsyncMock(return_value=quote_text)
        mock_api.get = AsyncMock(return_value=quote_data)
        mock_dt.now.return_value = now
        mock_status.return_value = status
        return await stock_snapshot({"symbol": "600519"}, _goal())


@pytest.mark.asyncio
async def test_raw_quote_mapped_from_chinese_keys():
    """中文键 dict → raw.quote 英文键映射（行情时间不映射）。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ev = await _run("【贵州茅台】最新价: 1500  涨跌幅: 1.2%", QUOTE_OK, now)
    assert ev.raw["quote"] == {
        "name": "贵州茅台",
        "code": "600519",
        "price": 1500.0,
        "change_pct": 1.2,
    }


@pytest.mark.asyncio
async def test_raw_quote_none_when_api_returns_none():
    """node_api.get 返回 None → raw.quote 为 None（不阻塞对话）。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ev = await _run("未找到股票 600519 的行情数据", None, now)
    assert ev.raw["quote"] is None
    assert ev.degraded is True


@pytest.mark.asyncio
async def test_raw_quote_present_when_degraded_non_trading():
    """非交易时段 degraded 时仍产出 quote（数据来自最近交易日）。"""
    now = datetime(2026, 8, 5, 8, 53, tzinfo=ZoneInfo("Asia/Shanghai"))  # 北京时间 08:53
    ev = await _run("【贵州茅台】最新价: 1500  涨跌幅: 1.2%", QUOTE_OK, now,
                    ("pre_open", "今日开盘前（开盘时间 09:30）"))
    assert ev.degraded is True
    assert "非交易时段" in (ev.degraded_reason or "")
    assert ev.raw["quote"]["name"] == "贵州茅台"


@pytest.mark.asyncio
async def test_raw_quote_missing_fields_omitted():
    """缺中文键字段 → 对应英文键省略（防御，值 None 不写入）。"""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ev = await _run("【贵州茅台】最新价: 1500  涨跌幅: 1.2%", {"最新价": 1500.0}, now)
    assert ev.raw["quote"] == {"price": 1500.0}
