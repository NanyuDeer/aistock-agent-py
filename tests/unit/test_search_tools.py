"""search_tools 测试 — tavily_finance_search 工具层"""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_tavily_finance_search_success():
    """tavily_finance_search 正常返回格式化新闻文本"""
    from aistock_agent.tools.search_tools import tavily_finance_search

    mock_result = {
        "results": [
            {
                "title": "美联储加息25基点",
                "content": "美联储宣布将联邦基金利率目标区间上调25个基点...",
                "url": "https://example.com/news/1",
            },
            {
                "title": "中国PMI数据公布",
                "content": "国家统计局公布6月制造业PMI为50.2...",
                "url": "https://example.com/news/2",
            },
        ]
    }

    with patch("aistock_agent.tools.search_tools.TavilyService.search", return_value=mock_result):
        result = await tavily_finance_search.ainvoke({"query": "美联储加息"})

    assert "美联储加息25基点" in result
    assert "中国PMI数据公布" in result
    assert "https://example.com/news/1" in result


@pytest.mark.asyncio
async def test_tavily_finance_search_no_results():
    """tavily_finance_search 无结果时返回提示文本"""
    from aistock_agent.tools.search_tools import tavily_finance_search

    with patch("aistock_agent.tools.search_tools.TavilyService.search", return_value={"results": []}):
        result = await tavily_finance_search.ainvoke({"query": "不存在的关键词"})

    assert "未找到" in result


@pytest.mark.asyncio
async def test_tavily_finance_search_api_error_degraded():
    """tavily_finance_search API 异常时返回降级文本（@safe_tool_call）"""
    from aistock_agent.tools.search_tools import tavily_finance_search

    with patch(
        "aistock_agent.tools.search_tools.TavilyService.search",
        side_effect=Exception("网络超时"),
    ):
        result = await tavily_finance_search.ainvoke({"query": "测试"})

    assert "搜索失败" in result or "不可用" in result
