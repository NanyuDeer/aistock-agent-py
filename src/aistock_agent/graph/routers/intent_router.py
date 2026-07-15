"""条件边函数 — supervisor 输出后根据 intent 路由到对应 Agent

当 trigger_source="user" 时，非 general/broadcast 的意图路由到 ai_advisor_agent，
从 DB 读取已有报告整理后直接回复（省 token，适合手机端简洁回复）。
"""

from aistock_agent.state.schema import AgentState

VALID_INTENTS = {"morning", "stock", "sector", "event", "wind_leader", "broadcast", "hot_burst", "alert", "ai_advisor", "general", "trend_score"}


def route_by_intent(state: AgentState) -> str:
    """根据 state.intent 路由到对应 Agent 节点

    当 trigger_source="user" 时，非 general/broadcast 的意图路由到 ai_advisor_agent，
    从 DB 读取已有报告整理回复（省 token）。

    Returns:
        节点名：morning_agent | stock_analyst | sector_analyst | event_analyst |
                wind_leader_agent | broadcast_agent | hot_burst_agent | alert_agent |
                ai_advisor_agent | general_agent
    """
    intent = state.get("intent", "general") or "general"
    if intent not in VALID_INTENTS:
        intent = "general"

    # 用户对话走 ai_advisor（复用 DB 报告，整理后直接回复）
    trigger_source = state.get("trigger_source")
    if trigger_source == "user" and intent not in ("general", "broadcast"):
        return "ai_advisor_agent"

    # intent → 节点名映射（scheduler 触发或其他情况）
    node_map = {
        "morning": "morning_agent",
        "stock": "stock_analyst",
        "sector": "sector_analyst",
        "event": "event_analyst",
        "wind_leader": "wind_leader_agent",
        "broadcast": "broadcast_agent",
        "general": "general_agent",
        "hot_burst": "hot_burst_agent",
        "alert": "alert_agent",
        "ai_advisor": "ai_advisor_agent",
        "trend_score": "trend_score_agent",
    }
    return node_map[intent]
