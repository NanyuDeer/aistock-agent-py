"""constants 模块测试 — SSE 事件类型 / 意图集合 / 错误码 / 工具标签"""

from aistock_agent.constants import (
    ERROR_CODES,
    INTENT_SET,
    TOOL_LABELS,
    SSEEventType,
)


def test_sse_event_type_values():
    # SSE 事件类型必须是约定的字符串字面量（前端按此解析）
    assert SSEEventType.TOOL_START == "tool_start"
    assert SSEEventType.TOOL_END == "tool_end"
    assert SSEEventType.LLM_START == "llm_start"
    assert SSEEventType.TEXT == "text"
    assert SSEEventType.DONE == "done"
    assert SSEEventType.ERROR == "error"


def test_intent_set_contents():
    # 与 graph/routers/intent_router.py 的 VALID_INTENTS 对齐
    assert INTENT_SET == frozenset({
        "morning", "stock", "sector", "event", "wind_leader", "broadcast",
        "hot_burst", "alert", "ai_advisor", "general", "trend_score", "review",
    })


def test_user_review_intent_routes_to_ai_advisor_agent():
    """用户查询盘后复盘时，路由到只消费持久化报告的投顾节点。"""
    from aistock_agent.graph.routers.intent_router import route_by_intent

    assert route_by_intent({"intent": "review", "trigger_source": "user"}) == "ai_advisor_agent"


def test_non_user_review_intent_routes_to_report_only_advisor_agent():
    """非用户路径也不能因 review 意图落入不存在的图节点。"""
    from aistock_agent.graph.routers.intent_router import route_by_intent

    assert (
        route_by_intent({"intent": "review", "trigger_source": "scheduler"})
        == "ai_advisor_agent"
    )


def test_error_codes_distinct():
    codes = {
        ERROR_CODES.DATA_UNAVAILABLE,
        ERROR_CODES.LLM_TIMEOUT,
        ERROR_CODES.TOOL_EXECUTION,
        ERROR_CODES.ROUTE,
    }
    assert len(codes) == 4


def test_tool_labels_has_morning_tools():
    # morning agent 的三个工具必须有中文标签（被 SSE tool_start 事件引用）
    assert TOOL_LABELS["get_global_markets"] == "正在获取全球市场行情"
    assert TOOL_LABELS["tavily_finance_search"] == "正在搜索财经新闻"
    assert TOOL_LABELS["get_cls_news"] == "正在获取财联社资讯"
