"""条件边函数 — supervisor 输出后根据 intent 路由到对应 Agent

路由表由 ``agents/registry.py`` 生成（单一真源），本模块只保留路由函数本身。
未知意图回落 general（历史行为，``tests/unit/test_constants.py`` 锁定）。
"""

from aistock_agent.agents.registry import agent_for_intent, get_agent, routable_intents
from aistock_agent.state.schema import AgentState

# 保留导出名：历史调用方/测试以 VALID_INTENTS 引用意图集合。
VALID_INTENTS = set(routable_intents())


def route_by_intent(state: AgentState) -> str:
    """根据 state.intent 路由到对应 Agent 节点名。

    Returns:
        registry 里该意图对应 agent 的 ``node_name``；未知意图 → ``general_agent``。

    注：意图 ``"review"`` 由 general 兜底（不是 review agent），见 registry 模块
    docstring 的历史遗留说明 —— 不要在此"修正"，该行为被测试锁死。
    """
    intent = state.get("intent", "general") or "general"
    spec = agent_for_intent(intent) or get_agent("general")
    return spec.node_name
