"""Tool Registry 测试 — 工具注册中心

验证 registry 三种使用模式：
- get_tools("category") 按 category 返回工具集
- get_tools() 不传参数返回全部工具（去重）
- 直接 import 具体工具名（通过 __all__ 导出）
"""

from aistock_agent.tools.registry import get_all_tools, get_tools, TOOL_REGISTRY


def test_get_tools_by_category_morning():
    """get_tools('morning') 返回晨报工具集"""
    tools = get_tools("morning")
    assert len(tools) == 3
    tool_names = [t.name for t in tools]
    assert "tavily_finance_search" in tool_names
    assert "get_global_markets" in tool_names
    assert "get_cls_news" in tool_names


def test_get_tools_by_category_stock():
    """get_tools('stock') 返回个股分析工具集"""
    tools = get_tools("stock")
    assert len(tools) == 4
    tool_names = [t.name for t in tools]
    assert "get_quote" in tool_names
    assert "get_capital_flow" in tool_names
    assert "get_profit_forecast" in tool_names
    assert "search_cls_news" in tool_names


def test_get_tools_by_category_sector():
    """get_tools('sector') 返回板块分析工具集"""
    tools = get_tools("sector")
    assert len(tools) == 2
    tool_names = [t.name for t in tools]
    assert "get_leader_stocks" in tool_names
    assert "get_capital_flow" in tool_names


def test_get_tools_by_category_event():
    """get_tools('event') 返回事件传导链分析工具集

    event agent 工具集：search_cls_news, get_news_fulltext, get_quote, tavily_finance_search
    （与 event.py 实际使用一致，顺序保持同步）
    """
    tools = get_tools("event")
    assert len(tools) == 4
    tool_names = [t.name for t in tools]
    assert "search_cls_news" in tool_names
    assert "get_news_fulltext" in tool_names
    assert "get_quote" in tool_names
    assert "tavily_finance_search" in tool_names


def test_get_tools_unknown_category_returns_empty():
    """get_tools 传未知 category 返回空列表"""
    tools = get_tools("nonexistent")
    assert tools == []


def test_get_tools_no_category_returns_all():
    """get_tools() 不传参数返回全部工具（去重）"""
    tools = get_tools()
    tool_names = [t.name for t in tools]
    # get_capital_flow 在 stock 和 sector 都注册了，去重后只出现一次
    assert tool_names.count("get_capital_flow") == 1
    assert len(tools) >= 7  # 至少7个唯一工具


def test_get_all_tools_deduplicated():
    """get_all_tools 返回去重后的全部工具"""
    tools = get_all_tools()
    tool_names = [t.name for t in tools]
    # 确认没有重复
    assert len(tool_names) == len(set(tool_names))


def test_registry_has_iterate_category():
    """registry 包含 iterate category（空列表，迭代agent无工具）"""
    assert "iterate" in TOOL_REGISTRY
    assert TOOL_REGISTRY["iterate"] == []


def test_get_tools_returns_same_object_references():
    """get_tools 返回的工具对象与直接 import 的是同一引用

    这是集成测试 tools_arg == EXPECTED_TOOLS 断言能通过的前提：
    registry 必须复用工具模块中的同一对象，而非副本。
    """
    from aistock_agent.tools.market_tools import get_global_markets
    from aistock_agent.tools.news_tools import get_cls_news, get_news_fulltext, search_cls_news
    from aistock_agent.tools.search_tools import tavily_finance_search
    from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

    morning_tools = get_tools("morning")
    # morning: [tavily_finance_search, get_global_markets, get_cls_news]
    assert morning_tools[0] is tavily_finance_search
    assert morning_tools[1] is get_global_markets
    assert morning_tools[2] is get_cls_news

    stock_tools = get_tools("stock")
    # stock: [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]
    assert stock_tools[0] is get_quote
    assert stock_tools[1] is get_capital_flow
    assert stock_tools[2] is get_profit_forecast
    assert stock_tools[3] is search_cls_news

    sector_tools = get_tools("sector")
    # sector: [get_leader_stocks, get_capital_flow]
    from aistock_agent.tools.sector_tools import get_leader_stocks
    assert sector_tools[0] is get_leader_stocks
    assert sector_tools[1] is get_capital_flow

    event_tools = get_tools("event")
    # event: [search_cls_news, get_news_fulltext, get_quote, tavily_finance_search]
    assert event_tools[0] is search_cls_news
    assert event_tools[1] is get_news_fulltext
    assert event_tools[2] is get_quote
    assert event_tools[3] is tavily_finance_search
