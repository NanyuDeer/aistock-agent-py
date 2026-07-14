"""搜索工具 — Tavily 全网财经搜索

从 market_tools.py 迁移而来，market_tools 回归纯 yfinance 行情职责。
实际 API 调用委托给 services/tavily.py 的 TavilyService。
"""

from typing import cast

from langchain_core.tools import tool

from aistock_agent.services.tavily import TavilyService
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def tavily_finance_search(query: str) -> str:
    """全网财经新闻搜索（Tavily），用于宏观事件/政策/经济数据搜索

    Args:
        query: 搜索关键词，如"美联储利率决议"、"中国PMI数据"
    """
    try:
        result = TavilyService.search(query=query, topic="news", max_results=5)

        if not result.get("results"):
            return f"未找到关于「{query}」的相关新闻"

        lines = []
        # TavilyService.search 返回 dict[str, object]，cast 声明 results 为列表
        results = cast(list[dict[str, str]], result["results"])
        for item in results:
            title = item.get("title", "无标题")
            content = item.get("content", "")[:200]
            url = item.get("url", "")
            lines.append(f"- {title}\n  {content}...\n  来源: {url}")
        return "\n".join(lines)
    except Exception as e:
        return f"Tavily 搜索失败: {e}"


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("morning", tavily_finance_search)
register("event", tavily_finance_search)
register("alert_news", tavily_finance_search)
# advisor agent 复用
register("advisor", tavily_finance_search)
