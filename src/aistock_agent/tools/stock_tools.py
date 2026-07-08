"""个股行情工具 — 通过 Node.js /internal/* API 获取 A 股数据"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
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
@safe_tool_call
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
@safe_tool_call
async def get_profit_forecast(symbol: str) -> str:
    """查询个股机构盈利预测

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/forecast/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的盈利预测数据"
    return _format_forecast(data)


def _format_quote(data: dict[str, object]) -> str:
    """格式化行情数据（腾讯数据源，中文 key）"""
    name = data.get("股票简称", "未知")
    price = data.get("最新价", "-")
    change_pct = data.get("涨跌幅", "-")
    return f"【{name}】最新价: {price}  涨跌幅: {change_pct}%"


def _format_capital_flow(data: dict[str, object]) -> str:
    """格式化资金流向数据（新浪字段：r0_*=主力, netamount=净额）"""
    main_in = data.get("r0_in", "-")
    main_out = data.get("r0_out", "-")
    net = data.get("netamount", "-")
    return (
        f"主力流入: {main_in}  主力流出: {main_out}\n"
        f"主力净流入: {net}"
    )


def _format_forecast(data: dict[str, object]) -> str:
    """格式化盈利预测数据（同花顺返回摘要 + 详细指标表）"""
    summary = str(data.get("摘要", ""))
    detail = data.get("业绩预测详表_详细指标预测", [])
    lines: list[str] = [summary] if summary else []
    if isinstance(detail, list):
        for row in detail:
            if not isinstance(row, dict):
                continue
            # 每行是 {预测指标, 2023-实际值, 2024-实际值, ..., 预测2026-平均, ...}
            indicator = row.get("预测指标", "")
            avg_2026 = row.get("预测2026-平均", "-")
            avg_2027 = row.get("预测2027-平均", "-")
            lines.append(f"  {indicator}: 2026预测={avg_2026}  2027预测={avg_2027}")
    return "\n".join(lines) if lines else "无盈利预测数据"


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("stock", get_quote)
register("stock", get_capital_flow)
register("stock", get_profit_forecast)
# get_quote 也被 event agent 使用
register("event", get_quote)
# get_capital_flow 也被 sector agent 使用
register("sector", get_capital_flow)
