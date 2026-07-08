"""十倍股评分工具 — 通过 Node.js /internal/tenx/* 获取评分数据

包含两个工具：
- ``get_tenx_score``：个股十倍股评分（6维度18指标百分制评分体系）
- ``get_tenx_top_stocks``：十倍股评分 Top 列表
"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_tenx_score(symbol: str) -> str:
    """查询个股十倍股评分（6维度18指标百分制评分体系）

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/tenx/score/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的十倍股评分数据"
    return _format_score(data)


@tool
@safe_tool_call
async def get_tenx_top_stocks(limit: int = 20) -> str:
    """查询十倍股评分 Top 列表

    Args:
        limit: 返回数量，默认20
    """
    data = await node_api.get(f"/internal/tenx/top?limit={limit}")
    if not data:
        return "暂无十倍股 Top 列表数据"
    return _format_top_stocks(data)


def _format_score(data: dict[str, object]) -> str:
    """格式化十倍股评分结果（TenxScoreResult）"""
    score = data.get("score", "-")
    label = data.get("label", "-")
    expected = data.get("expectedMultiple", "-")
    description = str(data.get("description", ""))
    ai_conclusion = str(data.get("aiConclusion", ""))

    lines: list[str] = [f"十倍股评分: {score}分（{label}级）  预期倍数: {expected}"]
    if ai_conclusion:
        lines.append(f"  {ai_conclusion}")
    if description:
        lines.append(f"  {description}")

    # 维度明细
    dimensions = data.get("dimensions", [])
    if isinstance(dimensions, list):
        lines.append("  维度明细：")
        for dim in dimensions:
            if not isinstance(dim, dict):
                continue
            d_name = dim.get("name", "-")
            d_weight = dim.get("weight", "-")
            d_score = dim.get("score", "-")
            lines.append(f"    - {d_name}({d_weight}%): {d_score}分")
    return "\n".join(lines)


def _format_top_stocks(data: dict[str, object]) -> str:
    """格式化十倍股 Top 列表"""
    stocks = data.get("stocks", [])
    if not isinstance(stocks, list) or not stocks:
        return "暂无十倍股 Top 列表数据"

    lines: list[str] = ["十倍股 Top 列表："]
    for i, stock in enumerate(stocks[:20], 1):
        if not isinstance(stock, dict):
            continue
        name = stock.get("name", "-")
        code = stock.get("symbol", "-")
        score = stock.get("score", "-")
        label = stock.get("label", "-")
        lines.append(f"  {i}. {name}({code}) 评分: {score}  等级: {label}")
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("general", get_tenx_score)
register("general", get_tenx_top_stocks)
