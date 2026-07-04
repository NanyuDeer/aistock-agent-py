"""个股行情工具 — 通过 Node.js /internal/* API 获取 A 股数据"""

from typing import Any

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api


@tool
async def get_quote(symbol: str) -> str:
    """查询 A 股个股实时行情

    Args:
        symbol: 6位股票代码，如 600519（贵州茅台）
    """
    data = await node_api.get(f"/internal/quote/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的行情数据"
    return _format_quote(data)


@tool
async def get_capital_flow(symbol: str) -> str:
    """查询个股资金流向

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/flow/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的资金流向数据"
    return _format_capital_flow(data)


@tool
async def get_profit_forecast(symbol: str) -> str:
    """查询个股机构盈利预测

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/forecast/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的盈利预测数据"
    return _format_forecast(data)


def _format_quote(data: dict[str, Any]) -> str:
    """格式化行情数据"""
    name = data.get("name", "未知")
    price = data.get("price", "-")
    change = data.get("change", "-")
    change_pct = data.get("change_pct", "-")
    volume = data.get("volume", "-")
    turnover = data.get("turnover", "-")
    return (
        f"【{name}】当前价: {price}  涨跌: {change} ({change_pct}%)\n"
        f"成交量: {volume}  成交额: {turnover}"
    )


def _format_capital_flow(data: dict[str, Any]) -> str:
    """格式化资金流向数据"""
    main_in = data.get("main_inflow", "-")
    main_out = data.get("main_outflow", "-")
    net = data.get("main_net", "-")
    return (
        f"主力流入: {main_in}  主力流出: {main_out}\n"
        f"主力净流入: {net}"
    )


def _format_forecast(data: dict[str, Any]) -> str:
    """格式化盈利预测数据"""
    year = data.get("year", "-")
    eps_forecast = data.get("eps_forecast", "-")
    rating = data.get("rating", "-")
    org_count = data.get("org_count", "-")
    return (
        f"年度: {year}  预测EPS: {eps_forecast}\n"
        f"评级: {rating}  机构数: {org_count}"
    )
