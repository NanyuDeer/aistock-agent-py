"""CHAT QA 子图构建。

独立 StateGraph(QuestionState)，不复用 supervisor 图。
拓扑：qa_router → skill_executor → synth_answer → END
"""
from __future__ import annotations

import structlog
from langgraph.graph import END, START, StateGraph

from aistock_agent.graph.nodes.qa_router import qa_router_node
from aistock_agent.graph.nodes.skill_executor import skill_executor_node
from aistock_agent.graph.nodes.synth_answer import synth_answer_node
from aistock_agent.state.chat_schema import QuestionState

logger = structlog.get_logger()


def compile_chat_graph():
    """构建并编译 CHAT QA 子图。

    返回 LangGraph RunnableBinding，支持 ainvoke / astream / astream_events。
    不挂 checkpointer（由调用方按需通过 config 注入）。
    """
    graph = StateGraph(QuestionState)

    graph.add_node("qa_router", qa_router_node)
    graph.add_node("skill_executor", skill_executor_node)
    graph.add_node("synth_answer", synth_answer_node)

    graph.add_edge(START, "qa_router")
    graph.add_edge("qa_router", "skill_executor")
    graph.add_edge("skill_executor", "synth_answer")
    graph.add_edge("synth_answer", END)

    compiled = graph.compile()
    logger.info("chat_graph.compiled")
    return compiled
