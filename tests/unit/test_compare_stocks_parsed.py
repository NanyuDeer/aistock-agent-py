"""compare_stocks raw.parsed 结构化字段单测（P11 线 3，spec §3.1/§3.4）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.compare_stocks import compare_stocks


def _goal() -> InsightGoal:
    return InsightGoal(question="对比 600519 和 000858", intent="compare_stocks")  # type: ignore[arg-type]


def _quote_text(name: str, price: str, pct: str) -> str:
    return f"【{name}】最新价: {price}  涨跌幅: {pct}%"


@pytest.mark.asyncio
async def test_parsed_success_two_symbols():
    """两标的成功 → parsed 结构化 dict（available=True，price/change_pct 数值化）。"""
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(side_effect=[
        _quote_text("贵州茅台", "1500.0", "+1.20"),
        _quote_text("五粮液", "120.0", "-0.80"),
    ])
    with patch("aistock_agent.skills.compare_stocks.get_quote", fake):
        ev = await compare_stocks({"symbols": ["600519", "000858"]}, _goal())
    assert ev.raw["parsed"] == [
        {"name": "贵州茅台", "code": "600519", "price": 1500.0,
         "change_pct": 1.2, "available": True},
        {"name": "五粮液", "code": "000858", "price": 120.0,
         "change_pct": -0.8, "available": True},
    ]
    assert any(f.startswith("对比结论") for f in ev.raw["quotes"])


@pytest.mark.asyncio
async def test_parsed_partial_failure_marks_unavailable():
    """部分失败 → 失败标的 available=False 且无价格字段。"""
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(side_effect=[
        _quote_text("贵州茅台", "1500.0", "+1.2"),
        "未找到股票 000858 的行情数据",
    ])
    with patch("aistock_agent.skills.compare_stocks.get_quote", fake):
        ev = await compare_stocks({"symbols": ["600519", "000858"]}, _goal())
    assert ev.degraded is True
    parsed = ev.raw["parsed"]
    assert parsed[0] == {"name": "贵州茅台", "code": "600519", "price": 1500.0,
                         "change_pct": 1.2, "available": True}
    assert parsed[1] == {"name": "000858", "code": "000858", "available": False}
    assert "price" not in parsed[1]
    assert "change_pct" not in parsed[1]


@pytest.mark.asyncio
async def test_parsed_all_failed_empty():
    """全部失败 → parsed 全部 available=False（无成功标的）。"""
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(side_effect=[
        "未找到股票 600519 的行情数据",
        "未找到股票 000858 的行情数据",
    ])
    with patch("aistock_agent.skills.compare_stocks.get_quote", fake):
        ev = await compare_stocks({"symbols": ["600519", "000858"]}, _goal())
    assert ev.degraded is True
    assert all(item["available"] is False for item in ev.raw["parsed"])
    assert len(ev.raw["parsed"]) == 2
