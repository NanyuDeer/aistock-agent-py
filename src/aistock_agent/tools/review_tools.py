"""复盘专用工具 — A股市场概览 + 板块涨跌明细

get_market_summary: yfinance 获取 A 股主要指数（上证/深证/创业板/科创50）
get_sector_performance: Node.js /internal/wind-leaders 获取板块涨跌 + 龙头股
"""

import yfinance as yf  # type: ignore[import-untyped]
from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call

# A 股主要指数 → yfinance Ticker 映射
A_SHARE_INDICES: dict[str, str] = {
    "上证指数": "000001.SS",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "科创50": "000688.SS",
}


@tool
@safe_tool_call
async def get_market_summary() -> str:
    """获取 A 股主要指数行情（上证指数/深证成指/创业板指/科创50），用于收盘复盘

    返回各指数的最新价、涨跌点数和涨跌幅。
    """
    symbols = list(A_SHARE_INDICES.values())
    tickers = yf.Tickers(" ".join(symbols))

    results: list[str] = []
    for name, symbol in A_SHARE_INDICES.items():
        try:
            ticker = tickers.tickers.get(symbol)
            if not ticker:
                results.append(f"{name}: 数据暂不可用")
                continue
            info = ticker.fast_info
            price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
            change = getattr(info, "regular_market_change", None)
            change_pct = getattr(info, "regular_market_change_percent", None)

            if price is not None:
                change_str = f" {change:+.2f} ({change_pct:+.2f}%)" if change_pct else ""
                results.append(f"{name}: {price:.2f}{change_str}")
            else:
                results.append(f"{name}: 数据暂不可用")
        except Exception:
            results.append(f"{name}: 数据暂不可用")

    return "\n".join(results)


@tool
@safe_tool_call
async def get_sector_performance() -> str:
    """获取板块涨跌明细（热门板块涨幅 + 龙头股），用于复盘板块归因

    数据来源：Node.js WindLeaderService，返回 top 热门板块及其龙头股。
    """
    data = await node_api.get("/internal/wind-leaders")
    if not data:
        return "暂无板块涨跌数据"

    sectors_raw = data.get("hot_sectors", [])
    if not isinstance(sectors_raw, list) or not sectors_raw:
        return "暂无板块涨跌数据"

    update_time = data.get("update_time", "")
    header = f"板块涨跌明细（更新: {update_time}）" if update_time else "板块涨跌明细"
    lines: list[str] = [header]

    for i, sector in enumerate(sectors_raw[:10], 1):
        if not isinstance(sector, dict):
            continue
        name = sector.get("name", "未知板块")
        today_change = sector.get("today_change", "-")
        leading_stock = sector.get("leading_stock", "-")
        lines.append(f"  {i}. {name} 涨幅: {today_change}%  龙头: {leading_stock}")

    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.market_tools import get_global_markets  # noqa: E402
from aistock_agent.tools.news_tools import get_cls_news  # noqa: E402
from aistock_agent.tools.registry import register  # noqa: E402
from aistock_agent.tools.search_tools import tavily_finance_search  # noqa: E402

# 复盘 agent 工具集：复用晨报/事件工具 + 新增专属复盘工具
# tavily_finance_search / get_global_markets / get_cls_news 已在各自主模块注册到
# 自身 category，此处跨分类注册到 "review"，register() 会自动去重。
register("review", tavily_finance_search)
register("review", get_global_markets)
register("review", get_cls_news)
register("review", get_market_summary)
register("review", get_sector_performance)
