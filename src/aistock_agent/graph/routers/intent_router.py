"""条件边函数 — supervisor 输出后根据 intent 路由到对应 Agent"""

from aistock_agent.state.schema import AgentState

VALID_INTENTS = {"morning", "stock", "sector", "event", "general"}


def route_by_intent(state: AgentState) -> str:
    """根据 state.intent 路由到对应 Agent 节点

    Returns:
        节点名：morning_agent | stock_analyst | sector_analyst | event_analyst | general_agent
    """
    intent = state.get("intent", "general") or "general"
    if intent not in VALID_INTENTS:
        intent = "general"

    # intent → 节点名映射
    node_map = {
        "morning": "morning_agent",
        "stock": "stock_analyst",
        "sector": "sector_analyst",
        "event": "event_analyst",
        "general": "general_agent",
    }
    return node_map[intent]
