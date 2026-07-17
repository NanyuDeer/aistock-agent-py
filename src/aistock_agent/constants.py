"""项目级常量 — SSE 事件类型、LangGraph 事件类型、意图集合、错误码、工具标签

集中管理 magic string，业务代码禁止硬编码事件类型 / 意图 / 错误码。
SSE 事件类型统一引用 ``SSEEventType``，LangGraph 原始事件类型引用
``LangGraphEventType``，异常 code 引用 ``ERROR_CODES``。
"""


class SSEEventType:
    """前端 SSE 事件类型常量（字符串常量类，避免 enum 复杂度）。

    被 ``utils.sse.map_langgraph_event_to_sse`` 与 ``agents.workers.morning.stream``
    引用，禁止在业务代码中写 magic string。
    """

    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    LLM_START = "llm_start"
    TEXT = "text"
    DONE = "done"
    ERROR = "error"
    AGENT_SWITCH = "agent_switch"
    INTERMEDIATE = "intermediate"


class LangGraphEventType:
    """LangGraph ``astream_events(version="v2")`` 原始事件类型常量。

    供 ``utils.sse.map_langgraph_event_to_sse`` 做事件分发，避免 magic string。
    """

    ON_TOOL_START = "on_tool_start"
    ON_TOOL_END = "on_tool_end"
    ON_CHAT_MODEL_STREAM = "on_chat_model_stream"


class WSEventType:
    """WebSocket 事件类型常量（字符串常量类）。

    被 ``api.ws.ws_chat`` 引用，禁止在业务代码中写 magic string。
    与 ``SSEEventType`` 语义独立（WS 协议 vs SSE 协议），不复用。
    """

    AGENT_RESPONSE = "agent_response"
    INTERMEDIATE = "intermediate"  # 中间进度（如"正在理解你的问题..."）
    LLM_START = "llm_start"        # LLM 开始生成
    TEXT = "text"                  # 逐 token 文本
    TOOL_START = "tool_start"      # 工具调用开始
    TOOL_END = "tool_end"          # 工具调用结束
    DONE = "done"
    ERROR = "error"


# 意图集合 —— 与 graph/routers/intent_router.py 的 VALID_INTENTS 对齐
INTENT_SET = frozenset({
    "morning", "stock", "sector", "event",
    "wind_leader", "hot_burst", "broadcast",
    "alert", "ai_advisor", "general",
    "trend_score",
})


class ErrorCodes:
    """错误码常量，对应 errors/exceptions.py 的异常类 ``code`` 属性。"""

    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    ROUTE = "ROUTE"


# 别名：保持 brief 约定的 ``constants.ERROR_CODES`` 公共名（UPPER_SNAKE 常量风格）
ERROR_CODES = ErrorCodes


# 工具名称 → 中文标签（供 SSE tool_start 事件展示给前端）
# morning agent 工具
TOOL_LABELS: dict[str, str] = {
    "get_global_markets": "正在获取全球市场行情",
    "tavily_finance_search": "正在搜索财经新闻",
    "get_cls_news": "正在获取财联社资讯",
    # stock agent 工具
    "get_quote": "正在查询个股行情",
    "get_capital_flow": "正在查询资金流向",
    "get_profit_forecast": "正在查询盈利预测",
    "search_cls_news": "正在搜索个股新闻",
    # sector agent 工具
    "get_leader_stocks": "正在查询板块龙头",
    # event agent 工具
    "get_news_fulltext": "正在获取新闻全文",
    # wind_leader agent 工具
    "get_wind_leaders": "正在获取风口龙头数据",
    # hot_burst agent 工具
    "get_hot_burst": "正在获取机构调研热门股",
    "get_hot_burst_history": "正在获取机构调研历史记录",
    # alert agent 工具
    "get_stock_monitor": "正在获取个股异动数据",
    "get_alert_history": "正在获取异动历史",
    # alert 子 Agent 工具（Phase 6）
    "get_concepts": "正在加载产业链概念列表",
    "get_graph_by_concept": "正在查询产业链上下游",
    # trend_score agent 工具
    "get_trend_score": "正在查询趋势股评分",
    "get_trend_score_detail": "正在获取趋势股评分详情",
    "get_trend_top_stocks": "正在查询趋势股排行",
}
