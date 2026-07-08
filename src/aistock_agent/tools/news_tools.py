"""新闻工具 — 通过 Node.js /internal/* API + Tavily 获取财经新闻"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def search_cls_news(symbol: str) -> str:
    """搜索财联社个股相关新闻

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/news/search/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的相关新闻"
    return _format_news_list(data)


@tool
@safe_tool_call
async def get_news_fulltext(news_id: str) -> str:
    """获取财联社新闻全文

    Args:
        news_id: 新闻ID
    """
    data = await node_api.get(f"/internal/news/fulltext/{news_id}")
    if not data:
        return f"未找到新闻 {news_id} 的全文"
    title = data.get("title", "无标题")
    content = data.get("content", "")
    return f"【{title}】\n{content}"


@tool
@safe_tool_call
async def get_cls_news(limit: int = 10) -> str:
    """获取财联社最新快讯（晨报用）

    Args:
        limit: 返回条数，默认10
    """
    data = await node_api.get(f"/internal/news/latest?limit={limit}")
    if not data:
        return "暂无财联社快讯"
    return _format_news_list(data)


def _format_news_list(data: dict[str, object]) -> str:
    """格式化新闻列表"""
    news_list = data.get("items", data.get("news", []))
    if not isinstance(news_list, list) or not news_list:
        return "暂无相关新闻"

    lines: list[str] = []
    for item in news_list[:10]:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "无标题")
        time_str = item.get("time", item.get("ctime", ""))
        brief_raw = item.get("brief", item.get("content", ""))
        brief = str(brief_raw)[:100]
        lines.append(f"- [{time_str}] {title}\n  {brief}...")
    return "\n".join(lines)
