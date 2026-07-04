"""板块工具 — 通过 Node.js /internal/* API 获取板块数据"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api


@tool
async def get_leader_stocks(tag_code: str) -> str:
    """查询板块龙头股

    Args:
        tag_code: 板块代码，如 BK0475（白酒）
    """
    data = await node_api.get(f"/internal/leader/{tag_code}")
    if not data:
        return f"未找到板块 {tag_code} 的龙头股数据"
    return _format_leaders(data)


def _format_leaders(data: dict) -> str:
    """格式化龙头股数据"""
    tag_name = data.get("tag_name", "未知板块")
    leaders = data.get("leaders", [])
    if not leaders:
        return f"板块【{tag_name}】暂无龙头股数据"

    lines = [f"板块【{tag_name}】龙头股："]
    for i, stock in enumerate(leaders[:5], 1):
        name = stock.get("name", "-")
        code = stock.get("code", "-")
        change_pct = stock.get("change_pct", "-")
        reason = stock.get("reason", "")
        lines.append(f"  {i}. {name}({code}) 涨跌: {change_pct}%  {reason}")
    return "\n".join(lines)
