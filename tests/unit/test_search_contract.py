"""契约回归（评审 B5 修订）：锁定 tavily_finance_search 工具输出格式逐字节不变。"""
import asyncio
from unittest.mock import patch

from aistock_agent.tools.search_tools import tavily_finance_search


def test_tavily_finance_search_output_format_stable():
    """固定 fixture 下输出格式 = `- {title}\n  {content[:200]}...\n  来源: {url}`。

    fixture content 不得以 `...` 结尾，否则 tool 会追加出 6 个点、断言错位。
    两个 `.search` 可用行都保留：一是 mock TavilyService.search 返回值形态，
    二是 rely on 现有 @safe_tool_call 对异常的降级（此处只验证成功路径）。
    """
    fixture_result = {
        "results": [{"title": "美联储加息", "content": "美联储宣布加息25个基点", "url": "https://example.com/1"}],
        "provider": "tavily", "outcome": "ok",
    }

    def fake_search(query, *, topic="news", max_results=5):
        return fixture_result

    with patch("aistock_agent.tools.search_tools.TavilyService.search", side_effect=fake_search):
        text = asyncio.run(tavily_finance_search.ainvoke({"query": "美联储利率"}))

    assert text.startswith("- 美联储加息\n  美联储宣布加息25个基点...\n  来源: https://example.com/1")
