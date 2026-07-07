"""StateGraph 构建 + compile()

图拓扑层：只管骨架，不含节点实现逻辑。
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aistock_agent.agents.general import node as general_agent
from aistock_agent.agents.supervisor import node as supervisor
from aistock_agent.agents.workers import event as event_analyst
from aistock_agent.agents.workers import morning as morning_agent
from aistock_agent.agents.workers import sector as sector_analyst
from aistock_agent.agents.workers import stock as stock_analyst
from aistock_agent.graph.routers.intent_router import route_by_intent
from aistock_agent.memory.checkpointer import get_checkpointer
from aistock_agent.state.schema import AgentState


class _Default:
    """哨兵类型：标记 compile_graph 未显式传 checkpointer。

    用于区分「未传参」（挂载默认 get_checkpointer()）与「显式传 None」
    （跳过 checkpointer，无多轮恢复）——单纯的 None 默认值无法区分两者。
    """


_DEFAULT = _Default()


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


def compile_graph(
    checkpointer: BaseCheckpointSaver[str] | None | _Default = _DEFAULT,
) -> CompiledStateGraph:
    """编译图（可直接调用 invoke / stream）。

    Args:
        checkpointer: LangGraph checkpointer。
            - 不传（默认）：挂载 ``get_checkpointer()``，启用多轮对话恢复。
            - 传 ``None``：显式跳过 checkpointer（无多轮恢复）。
            - 传 saver 实例：使用该 saver。

    build_graph() 保持纯拓扑、不挂载 checkpointer；只有 compile_graph() 挂载。
    """
    if isinstance(checkpointer, _Default):
        checkpointer = get_checkpointer()
    return build_graph().compile(checkpointer=checkpointer)
