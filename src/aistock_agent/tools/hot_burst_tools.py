"""机构调研推荐热门股工具 — 通过 Node.js /internal/institution-research* 获取

包含两个工具：
- ``get_hot_burst``：机构调研推荐热门股检测结果（四信号源共振模型）
- ``get_hot_burst_history``：历史检测记录（从数据库分页查询）
"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_hot_burst(limit: int = 20) -> str:
    """查询机构调研推荐热门股检测结果（四信号源共振模型），用于热点挖掘

    Args:
        limit: 返回数量，默认20
    """
    data = await node_api.get(f"/internal/institution-research?limit={limit}")
    if not data:
        return "暂无机构调研推荐热门股数据"
    return _format_hot_burst(data)


@tool
@safe_tool_call
async def get_hot_burst_history(days: int = 30) -> str:
    """查询机构调研推荐热门股历史记录

    Args:
        days: 查询天数，默认30
    """
    data = await node_api.get(f"/internal/institution-research/history?days={days}")
    if not data:
        return "暂无历史记录数据"
    return _format_history(data)


def _format_hot_burst(data: dict[str, object]) -> str:
    """格式化机构调研推荐热门股检测结果（HotBurstResult）"""
    update_time = data.get("update_time", "")
    outbreaks = data.get("outbreaks", [])

    header = f"机构调研推荐热门股（更新: {update_time}）" if update_time else "机构调研推荐热门股"
    if not isinstance(outbreaks, list) or not outbreaks:
        return f"{header}：暂无共振信号"

    lines: list[str] = [header]
    for i, stock in enumerate(outbreaks[:20], 1):
        if not isinstance(stock, dict):
            continue
        name = stock.get("stockName", "-")
        code = stock.get("symbol", "-")
        resonance = stock.get("resonanceCount", "-")
        level = stock.get("resonanceLevel", "-")
        price = stock.get("price", "-")
        change_pct = stock.get("changePct", "-")
        sector = stock.get("sectorInfo", "")
        lines.append(
            f"  {i}. {name}({code}) 共振: {resonance}源[{level}]  "
            f"价格: {price}  涨跌: {change_pct}%  {sector}"
        )
    return "\n".join(lines)


def _format_history(data: dict[str, object]) -> str:
    """格式化历史记录"""
    records = data.get("records", [])
    if not isinstance(records, list) or not records:
        return "暂无历史记录数据"

    lines: list[str] = [f"历史记录（共 {data.get('total', len(records))} 条）："]
    for i, record in enumerate(records[:20], 1):
        if not isinstance(record, dict):
            continue
        name = record.get("stock_name", "-")
        code = record.get("stock_code", "-")
        date = record.get("push_date", "-")
        theme = record.get("theme", "")
        resonance = record.get("resonance_count", "-")
        price = record.get("push_price", "-")
        lines.append(
            f"  {i}. [{date}] {name}({code}) 主题: {theme}  "
            f"共振: {resonance}源  推荐价: {price}"
        )
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("general", get_hot_burst)
register("general", get_hot_burst_history)
