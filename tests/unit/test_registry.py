"""Tool Registry 测试 — 工具注册中心

验证 registry 三种使用模式：
- get_tools("category") 按 category 返回工具集
- get_tools() 不传参数返回全部工具（去重）
- 直接 import 具体工具名
- register(expose=False) 控制 /skills 接口暴露

注意：自动注册机制下工具顺序由模块导入顺序决定，
测试使用集合断言（set）而非列表断言，避免顺序依赖。
"""

from aistock_agent.tools.registry import get_all_tools, get_exposed_skills, get_tools


def test_get_tools_by_category_morning():
    """get_tools('morning') 返回晨报工具集"""
    tools = get_tools("morning")
    tool_names = {t.name for t in tools}
    assert "tavily_finance_search" in tool_names
    assert "get_global_markets" in tool_names
    assert "get_cls_news" in tool_names


def test_get_tools_by_category_stock():
    """get_tools('stock') 返回个股分析工具集"""
    tools = get_tools("stock")
    tool_names = {t.name for t in tools}
    assert "get_quote" in tool_names
    assert "get_capital_flow" in tool_names
    assert "get_profit_forecast" in tool_names
    assert "search_cls_news" in tool_names


def test_get_tools_by_category_sector():
    """get_tools('sector') 返回板块分析工具集"""
    tools = get_tools("sector")
    tool_names = {t.name for t in tools}
    assert "get_leader_stocks" in tool_names
    assert "get_capital_flow" in tool_names


def test_get_tools_by_category_event():
    """get_tools('event') 返回事件传导链分析工具集

    event agent 工具集：search_cls_news, get_news_fulltext, get_quote, tavily_finance_search
    """
    tools = get_tools("event")
    tool_names = {t.name for t in tools}
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


def test_get_tools_iterate_category_empty():
    """get_tools('iterate') 返回空列表（迭代 agent 无工具）"""
    tools = get_tools("iterate")
    assert tools == []


def test_get_exposed_skills_deduplicated():
    """get_exposed_skills 返回按注册顺序去重的工具"""
    exposed = get_exposed_skills()
    names = [t.name for t in exposed]
    assert len(names) == len(set(names))


def test_get_exposed_skills_excludes_non_exposed():
    """register(expose=False) 的工具不出现在 get_exposed_skills 中"""
    from langchain_core.tools import tool as tool_decorator

    from aistock_agent.tools.registry import register

    @tool_decorator
    def _internal_tool_for_test() -> str:
        """内部测试工具，不应暴露给 /skills"""
        return "internal"

    # 注册到一个临时 category，不暴露
    register("__test_internal", _internal_tool_for_test, expose=False)

    exposed_names = {t.name for t in get_exposed_skills()}
    assert "_internal_tool_for_test" not in exposed_names

    # 但 get_tools 能拿到
    tools = get_tools("__test_internal")
    assert len(tools) == 1


def test_get_tools_returns_same_object_references():
    """get_tools 返回的工具对象与直接 import 的是同一引用

    registry 必须复用工具模块中的同一对象，而非副本。
    """
    from aistock_agent.tools.market_tools import get_global_markets
    from aistock_agent.tools.news_tools import get_cls_news, get_news_fulltext, search_cls_news
    from aistock_agent.tools.search_tools import tavily_finance_search
    from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

    morning_tools = get_tools("morning")
    morning_refs = {id(t) for t in morning_tools}
    assert id(tavily_finance_search) in morning_refs
    assert id(get_global_markets) in morning_refs
    assert id(get_cls_news) in morning_refs

    stock_tools = get_tools("stock")
    stock_refs = {id(t) for t in stock_tools}
    assert id(get_quote) in stock_refs
    assert id(get_capital_flow) in stock_refs
    assert id(get_profit_forecast) in stock_refs
    assert id(search_cls_news) in stock_refs

    sector_tools = get_tools("sector")
    sector_refs = {id(t) for t in sector_tools}
    from aistock_agent.tools.sector_tools import get_leader_stocks
    assert id(get_leader_stocks) in sector_refs
    assert id(get_capital_flow) in sector_refs

    event_tools = get_tools("event")
    event_refs = {id(t) for t in event_tools}
    assert id(search_cls_news) in event_refs
    assert id(get_news_fulltext) in event_refs
    assert id(get_quote) in event_refs
    assert id(tavily_finance_search) in event_refs
