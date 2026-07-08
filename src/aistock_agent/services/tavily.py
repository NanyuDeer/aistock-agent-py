"""Tavily 客户端封装层

将 Tavily API 调用从 tools/market_tools.py 抽出，
形成独立 service 层，供 tools/search_tools.py 调用。

Key 轮换逻辑复用 config.settings.get_tavily_key()。
"""

from typing import cast

from tavily import TavilyClient  # type: ignore[import-untyped]

from aistock_agent.config import settings


class TavilyService:
    """Tavily 搜索服务封装"""

    @staticmethod
    def search(query: str, *, topic: str = "news", max_results: int = 5) -> dict[str, object]:
        """统一搜索入口

        Args:
            query: 搜索关键词
            topic: 搜索类型（news / general）
            max_results: 最大结果数

        Returns:
            Tavily API 原始返回的 dict

        Raises:
            Exception: API 调用失败时抛出（由上层 @safe_tool_call 降级处理）
        """
        client = TavilyClient(api_key=settings.get_tavily_key())
        # tavily-python 无类型存根，client.search 返回 Any；cast 显式声明为 dict
        raw = client.search(query=query, topic=topic, max_results=max_results)
        return cast(dict[str, object], raw)
