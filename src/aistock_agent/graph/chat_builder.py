"""CHAT QA 子图构建。

独立 StateGraph(QuestionState)，不复用 supervisor 图。
拓扑（P1 D31）：qa_router → conditional → (escalate | skill_executor) → synth_answer → END
"""
from __future__ import annotations

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from aistock_agent.graph.nodes.escalate import escalate_node
from aistock_agent.graph.nodes.general_fallback import general_fallback_node
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


def route_after_router(state: QuestionState) -> str:
    """qa_router 后条件路由（D31 拓扑）：deep 且无短路 → escalate，否则 skill_executor。

    - deep 分支前提：complexity=="deep" 且未写 final_response/clarification
      （护栏优先：闸门短路写 final_response / 澄清写 clarification 时仍走 light 路径）
    - 其余（light / 短路 / 澄清）→ skill_executor：闸门短路 skill_calls 为空 →
      skill_executor 返回空 evidences → synth_answer 直接透出，行为不变
    - 防御（2026-08-02 审计发现）：conditional 边 state 由 LangGraph reader 经
      `asyncio.to_thread` 读取；测试若全局 patch 该函数（如 test_e2e_market_snapshot
      的 AsyncMock），reader 会返回非 dict → 此处保守回落 light 路径，不中断图执行。
      生产环境 reader 恒返回 dict，本分支不会触发。
    """
    if not isinstance(state, dict):
        return "skill_executor"
    # P7+P8（D37/D32）：general_source 非空（科普/能力缺口）→ general_fallback 兜底，
    # 优先于 deep 判断（科普/缺口绝不升级 deep）
    if state.get("general_source"):
        return "general_fallback"
    if (
        state.get("complexity") == "deep"
        and not state.get("final_response")
        and not state.get("clarification")
    ):
        return "escalate"
    return "skill_executor"


def route_after_escalate(state: QuestionState) -> str:
    """escalate 后条件路由（D24）：fallback_to_skill → skill_executor，否则 synth_answer。

    防御同 route_after_router：state 非 dict（reader 被全局 patch 破坏）时走默认成功路径。
    """
    if not isinstance(state, dict):
        return "synth_answer"
    if state.get("fallback_to_skill"):
        return "skill_executor"
    return "synth_answer"


def build_chat_graph() -> StateGraph:
    """构建 CHAT QA 状态图（纯拓扑，不挂载 checkpointer）。"""
    graph = StateGraph(QuestionState)

    graph.add_node("qa_router", qa_router_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("skill_executor", skill_executor_node)
    graph.add_node("synth_answer", synth_answer_node)
    graph.add_node("general_fallback", general_fallback_node)

    graph.add_edge(START, "qa_router")
    graph.add_conditional_edges(
        "qa_router",
        route_after_router,
        {
            "escalate": "escalate",
            "skill_executor": "skill_executor",
            "general_fallback": "general_fallback",
        },
    )
    graph.add_conditional_edges(
        "escalate",
        route_after_escalate,
        {"skill_executor": "skill_executor", "synth_answer": "synth_answer"},
    )
    graph.add_edge("skill_executor", "synth_answer")
    graph.add_edge("general_fallback", "synth_answer")
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
