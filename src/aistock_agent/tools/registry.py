"""工具注册中心 — 按 category 集中管理工具集

agent 只需声明 category，即可获取完整工具列表，
不再手动 import + 拼接。

三种使用方式：
    # 方式1：默认导入全部
    from aistock_agent.tools.registry import get_tools
    tools = get_tools()

    # 方式2：按 category 命名控制
    tools = get_tools("morning")

    # 方式3：直接 import 具体工具名
    from aistock_agent.tools.registry import get_global_markets

工具顺序约束：每个 category 的工具顺序必须与对应 agent 中
原 tools 列表顺序一致，因为集成测试使用 ``tools == EXPECTED_TOOLS``
断言（含顺序）。修改顺序前请同步更新对应集成测试。
"""

from aistock_agent.tools.market_tools import get_global_markets
from aistock_agent.tools.news_tools import get_cls_news, get_news_fulltext, search_cls_news
from aistock_agent.tools.search_tools import tavily_finance_search
from aistock_agent.tools.sector_tools import get_leader_stocks
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

# 按 category 分组
# 顺序必须与各 agent 中原 tools 列表顺序一致（集成测试含顺序断言）
TOOL_REGISTRY: dict[str, list] = {
    "morning": [tavily_finance_search, get_global_markets, get_cls_news],
    "stock": [get_quote, get_capital_flow, get_profit_forecast, search_cls_news],
    "sector": [get_leader_stocks, get_capital_flow],
    "event": [search_cls_news, get_news_fulltext, get_quote, tavily_finance_search],
    # review / iterate category 在复盘/迭代 agent 实现时注册
    "iterate": [],  # 迭代agent无工具，纯读文件+LLM推理
}

__all__ = [
    "get_global_markets",
    "tavily_finance_search",
    "get_cls_news",
    "search_cls_news",
    "get_news_fulltext",
    "get_quote",
    "get_capital_flow",
    "get_profit_forecast",
    "get_leader_stocks",
    "get_tools",
    "get_all_tools",
    "TOOL_REGISTRY",
]


def get_all_tools() -> list:
    """获取全部工具（去重）

    Returns:
        去重后的全部工具列表，顺序按 TOOL_REGISTRY 遍历顺序
    """
    seen: set[int] = set()
    result: list = []
    for tools in TOOL_REGISTRY.values():
        for tool in tools:
            if id(tool) not in seen:
                seen.add(id(tool))
                result.append(tool)
    return result


def get_tools(category: str | None = None) -> list:
    """获取工具集

    Args:
        category: 工具分类名（如 "morning"、"stock"、"event"）。
                  不传或传 None → 返回全部工具（去重）。
                  传具体名称 → 返回该分类的工具列表。

    Returns:
        该分类的工具列表，未知 category 返回空列表
    """
    if category is None:
        return get_all_tools()
    return TOOL_REGISTRY.get(category, [])
