"""StateGraph 构建 + compile()

图拓扑层：只管骨架，不含节点实现逻辑。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from aistock_agent.agents import (
    event_analyst,
    general_agent,
    morning_agent,
    sector_analyst,
    stock_analyst,
    supervisor,
)
from aistock_agent.graph.edges import route_by_intent
from aistock_agent.state.schema import AgentState


def build_graph() -> StateGraph:
    """构建 LangGraph 状态图

    拓扑：
        START → supervisor → [条件路由] → 各 Agent 节点 → END
    """
    graph = StateGraph(AgentState)

    # 注册节点
    graph.add_node("supervisor", supervisor.run)
    graph.add_node("morning_agent", morning_agent.run)
    graph.add_node("stock_analyst", stock_analyst.run)
    graph.add_node("sector_analyst", sector_analyst.run)
    graph.add_node("event_analyst", event_analyst.run)
    graph.add_node("general_agent", general_agent.run)

    # 边：START → supervisor
    graph.add_edge(START, "supervisor")

    # 条件边：supervisor → 各 Agent
    graph.add_conditional_edges("supervisor", route_by_intent)

    # 各 Agent → END
    agent_nodes = [
        "morning_agent", "stock_analyst", "sector_analyst",
        "event_analyst", "general_agent",
    ]
    for node in agent_nodes:
        graph.add_edge(node, END)

    return graph


def compile_graph() -> Any:
    """编译图（可直接调用 invoke / stream）"""
    return build_graph().compile()
