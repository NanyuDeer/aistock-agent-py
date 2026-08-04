"""compare_stocks skill 单测（P5 D40）"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.compare_stocks import MAX_COMPARE_SYMBOLS, compare_stocks


def _goal(symbols: list[str]) -> InsightGoal:
    return InsightGoal(question="对比", intent="compare_stocks", symbols=symbols)  # type: ignore[arg-type]


def _quote_text(name: str, price: str, pct: str) -> str:
    return f"【{name}】最新价: {price}  涨跌幅: {pct}%"


@pytest.mark.asyncio
async def test_compare_two_symbols():
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(side_effect=[
        _quote_text("贵州茅台", "1500.0", "+1.20"),
        _quote_text("五粮液", "120.0", "-0.80"),
    ])
    with patch("aistock_agent.skills.compare_stocks.get_quote", fake):
        ev: Evidence = await compare_stocks({"symbols": ["600519", "000858"]}, _goal([]))
    assert ev.skill_name == "compare_stocks"
    assert not ev.degraded
    assert len(ev.facts) == 3  # 两标的一行 + 对比结论行
    assert any("600519" in f or "贵州茅台" in f for f in ev.facts)
    assert len(ev.sources) == 2
    assert ev.raw["compared"] == ["600519", "000858"]


@pytest.mark.asyncio
async def test_compare_less_than_two_raises():
    # 裸函数（@skill 装饰后异常→degraded Evidence，测试原函数守卫）
    with pytest.raises(ValueError):
        await compare_stocks.__wrapped__({"symbols": ["600519"]}, _goal([]))


@pytest.mark.asyncio
async def test_compare_truncates_over_max():
    symbols = [f"60051{i}" for i in range(7)]
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(return_value=_quote_text("x", "1", "0"))
    with patch("aistock_agent.skills.compare_stocks.get_quote", fake):
        ev = await compare_stocks({"symbols": symbols}, _goal([]))
    assert len(ev.raw["compared"]) == MAX_COMPARE_SYMBOLS


@pytest.mark.asyncio
async def test_compare_partial_failure_degraded():
    fake = AsyncMock()
    fake.ainvoke = AsyncMock(
        side_effect=[
            _quote_text("贵州茅台", "1500.0", "+1.2"),
            "未找到股票 000858 的行情数据",
        ]
    )
    with patch("aistock_agent.skills.compare_stocks.get_quote", fake):
        ev = await compare_stocks({"symbols": ["600519", "000858"]}, _goal([]))
    assert ev.degraded is True
    assert len(ev.facts) >= 2  # 失败标的不丢弃，标注不可用
