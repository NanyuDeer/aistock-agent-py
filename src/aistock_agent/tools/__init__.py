"""Tools 层 — 导入所有 tool 模块以触发 register() 自注册

导入本包（或从本包的任何子模块 import）时，__init__.py 会先运行，
确保所有 tool 模块被加载、register() 调用被执行，
之后 get_tools("category") 才能返回正确结果。
"""

# 导入所有含 @tool 工具的模块，触发 register() 调用
from aistock_agent.tools import (  # noqa: F401
    graph_tools,
    hot_burst_tools,
    industry_vector_search,
    market_tools,
    monitor_tools,
    news_tools,
    review_tools,
    search_tools,
    sector_tools,
    stock_tools,
    tenx_tools,
)
