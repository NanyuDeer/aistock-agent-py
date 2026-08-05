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
    """格式化风口龙头数据 → 长线链简报 + 短线链简报（供早上风口龙头 agent 生成综合报告）"""
    update_time = data.get("update_time", "")
    sectors_raw = data.get("hot_sectors", [])
    if not isinstance(sectors_raw, list) or not sectors_raw:
        return "暂无风口龙头数据"
    lines = [f"风口龙头（更新: {update_time}）"] if update_time else ["风口龙头"]
    long_board = [s for s in sectors_raw if isinstance(s, dict) and s.get("cycle") in ("long", "both")]
    short_board = [s for s in sectors_raw if isinstance(s, dict) and (s.get("cycle") or "short") in ("short", "both")]

    def fmt_sector(s: dict[str, object]) -> str:
        ai = s.get("ai_analysis") or {}
        if not isinstance(ai, dict):
            ai = {}
        name = s.get("name", "未知板块")
        today = s.get("today_change", "-")
        leader = s.get("leading_stock", "-")
        return (f"{name} 今日涨幅{today}% 龙头{leader} "
                f"长线{ai.get('long_term_days', 0)}天/置信{ai.get('long_confidence', 0)}：{ai.get('long_reason', '-')} "
                f"短线{ai.get('short_term_days', 0)}天/热度{ai.get('short_heat', 0)}：{ai.get('short_reason', '-')}")

    if long_board:
        lines.append("【长线链研判】")
        for i, s in enumerate(long_board[:8], 1):
            lines.append(f"  {i}. {fmt_sector(s)}")
    if short_board:
        lines.append("【短线链研判】")
        for i, s in enumerate(short_board[:8], 1):
            lines.append(f"  {i}. {fmt_sector(s)}")
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("sector", get_leader_stocks)
register("sector", get_wind_leaders)
register("wind_leader", get_wind_leaders)
# advisor agent 复用
register("advisor", get_leader_stocks)
