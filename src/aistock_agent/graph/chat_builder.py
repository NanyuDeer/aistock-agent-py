"""CHAT QA 子图构建。

独立 StateGraph(QuestionState)，不复用 supervisor 图。
拓扑：qa_router → skill_executor → synth_answer → END
"""
from __future__ import annotations

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver

from aistock_agent.graph.nodes.qa_router import qa_router_node
from aistock_agent.graph.nodes.skill_executor import skill_executor_node
from aistock_agent.graph.nodes.synth_answer import synth_answer_node
from aistock_agent.memory.checkpointer import get_checkpointer
from aistock_agent.state.chat_schema import QuestionState

logger = structlog.get_logger()


class _Default:
    """哨兵类型：标记 compile_chat_graph 未显式传 checkpointer。

    用于区分「未传参」（挂载默认 get_checkpointer()）与「显式传 None」
    （跳过 checkpointer，无多轮恢复）——单纯的 None 默认值无法区分两者。
    """


_DEFAULT = _Default()


def build_chat_graph() -> StateGraph:
    """构建 CHAT QA 状态图（纯拓扑，不挂载 checkpointer）。"""
    graph = StateGraph(QuestionState)

    graph.add_node("qa_router", qa_router_node)
    graph.add_node("skill_executor", skill_executor_node)
    graph.add_node("synth_answer", synth_answer_node)

    graph.add_edge(START, "qa_router")
    graph.add_edge("qa_router", "skill_executor")
    graph.add_edge("skill_executor", "synth_answer")
    graph.add_edge("synth_answer", END)

    return graph


def compile_chat_graph(
    checkpointer: BaseCheckpointSaver[str] | None | _Default = _DEFAULT,
):
    """构建并编译 CHAT QA 子图。

    Args:
        checkpointer: LangGraph checkpointer。
            - 不传（默认）：挂载 ``get_checkpointer()``，启用多轮对话恢复。
            - 传 ``None``：显式跳过 checkpointer（无多轮恢复）。
            - 传 saver 实例：使用该 saver。

    Returns:
        LangGraph CompiledStateGraph，支持 ainvoke / astream / astream_events。
    """
    if isinstance(checkpointer, _Default):
        checkpointer = get_checkpointer()

    compiled = build_chat_graph().compile(checkpointer=checkpointer)
    logger.info("chat_graph.compiled", has_checkpointer=checkpointer is not None)
    return compiled
