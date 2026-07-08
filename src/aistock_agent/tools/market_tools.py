"""市场工具 — yfinance 境外市场 + Tavily 全网搜索

这些工具在 Python 侧直接调用，Node.js 无对应实现。
"""


import yfinance as yf  # type: ignore[import-untyped]
from langchain_core.tools import tool

from aistock_agent.config import settings
from aistock_agent.tools.base import safe_tool_call

# yfinance Ticker 映射
GLOBAL_MARKET_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "kweb": "KWEB",          # 中概ETF
    "nikkei": "^N225",        # 日经
    "hsi": "^HSI",            # 恒生
    "kospi": "^KS11",         # 韩综
    "gold": "GC=F",
    "crude": "CL=F",
    "usdcny": "USDCNY=X",
}


@tool
@safe_tool_call
async def get_global_markets() -> str:
    """获取全球市场行情（美股/亚太/大宗/汇率），用于晨报宏观分析"""
    try:
        symbols = list(GLOBAL_MARKET_TICKERS.values())
        tickers = yf.Tickers(" ".join(symbols))

        results = []
        for name, symbol in GLOBAL_MARKET_TICKERS.items():
            try:
                ticker = tickers.tickers.get(symbol)
                if not ticker:
                    continue
                info = ticker.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                change = getattr(info, "regular_market_change", None)
                change_pct = getattr(info, "regular_market_change_percent", None)

                display_name = _market_display_name(name)
                if price is not None:
                    change_str = f" {change:+.2f} ({change_pct:+.2f}%)" if change_pct else ""
                    results.append(f"{display_name}: {price:.2f}{change_str}")
                else:
                    results.append(f"{display_name}: 数据暂不可用")
            except Exception:
                results.append(f"{_market_display_name(name)}: 获取失败")

        return "\n".join(results)
    except Exception as e:
        return f"全球市场数据获取失败: {e}"


@tool
@safe_tool_call
async def tavily_finance_search(query: str) -> str:
    """全网财经新闻搜索（Tavily），用于宏观事件/政策/经济数据搜索

    Args:
        query: 搜索关键词，如"美联储利率决议"、"中国PMI数据"
    """
    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]

        client = TavilyClient(api_key=settings.get_tavily_key())
        result = client.search(query=query, topic="news", max_results=5)

        if not result.get("results"):
            return f"未找到关于「{query}」的相关新闻"

        lines = []
        for item in result["results"]:
            title = item.get("title", "无标题")
            content = item.get("content", "")[:200]
            url = item.get("url", "")
            lines.append(f"- {title}\n  {content}...\n  来源: {url}")
        return "\n".join(lines)
    except Exception as e:
        return f"Tavily 搜索失败: {e}"


def _market_display_name(key: str) -> str:
    """将 key 映射为中文显示名"""
    names = {
        "sp500": "标普500",
        "nasdaq": "纳斯达克",
        "dow": "道琼斯",
        "kweb": "中概ETF(KWEB)",
        "nikkei": "日经225",
        "hsi": "恒生指数",
        "kospi": "韩国综合",
        "gold": "黄金",
        "crude": "原油",
        "usdcny": "美元/人民币",
    }
    return names.get(key, key)
