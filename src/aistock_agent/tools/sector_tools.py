"""板块工具 — 通过 Node.js /internal/* API 获取板块数据"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_leader_stocks(tag_code: str) -> str:
    """查询板块龙头股

    Args:
        tag_code: 板块代码，如 BK0475（白酒）
    """
    data = await node_api.get(f"/internal/leader/{tag_code}")
    if not data:
        return f"未找到板块 {tag_code} 的龙头股数据"
    return _format_leaders(data)


@tool
@safe_tool_call
async def get_wind_leaders() -> str:
    """查询风口龙头分析数据（热门板块 + 各板块龙头股），用于市场情绪与主线研判

    数据来源：Node.js WindLeaderService，返回 top 热门板块及其 main_stocks。
    """
    data = await node_api.get("/internal/wind-leaders")
    if not data:
        return "暂无风口龙头数据"
    return _format_wind_leaders(data)


def _format_leaders(data: dict[str, object]) -> str:
    """格式化龙头股数据（Tushare 返回 tag_code + leaders 数组）"""
    tag_name = data.get("tag_code", data.get("tag_name", "未知板块"))
    leaders_raw = data.get("leaders", [])
    if not isinstance(leaders_raw, list) or not leaders_raw:
        return f"板块【{tag_name}】暂无龙头股数据"

    lines = [f"板块【{tag_name}】龙头股："]
    for i, stock in enumerate(leaders_raw[:5], 1):
        if not isinstance(stock, dict):
            continue
        name = stock.get("name", "-")
        code = stock.get("code", "-")
        change_pct = stock.get("change_pct", "-")
        reason = stock.get("reason", "")
        lines.append(f"  {i}. {name}({code}) 涨跌: {change_pct}%  {reason}")
    return "\n".join(lines)


def _format_wind_leaders(data: dict[str, object]) -> str:
    """格式化风口龙头数据（WindLeaderService 返回 update_time + hot_sectors）"""
    update_time = data.get("update_time", "")
    sectors_raw = data.get("hot_sectors", [])
    if not isinstance(sectors_raw, list) or not sectors_raw:
        return "暂无风口龙头数据"

    header = f"风口龙头（更新: {update_time}）" if update_time else "风口龙头"
    lines: list[str] = [header]
    for i, sector in enumerate(sectors_raw[:8], 1):
        if not isinstance(sector, dict):
            continue
        name = sector.get("name", "未知板块")
        today_change = sector.get("today_change", "-")
        leading_stock = sector.get("leading_stock", "-")
        lines.append(f"  {i}. {name} 涨幅: {today_change}%  龙头: {leading_stock}")
        # 列出该板块的核心推荐股
        main_stocks = sector.get("main_stocks", [])
        if isinstance(main_stocks, list):
            for stock in main_stocks[:3]:
                if not isinstance(stock, dict):
                    continue
                s_name = stock.get("name", "-")
                s_code = stock.get("code", "-")
                s_change = stock.get("change_pct", "-")
                lines.append(f"      - {s_name}({s_code}) 涨跌: {s_change}%")
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("sector", get_leader_stocks)
register("sector", get_wind_leaders)
register("wind_leader", get_wind_leaders)
# advisor agent 复用
register("advisor", get_leader_stocks)
