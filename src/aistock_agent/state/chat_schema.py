"""CHAT QA 链路独立状态。

不复用 AgentState，对齐 spec §2.3。
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    Evidence,
    Insight,
    InsightGoal,
    SkillCall,
)


class QuestionState(TypedDict, total=False):
    """CHAT QA 链路状态。

    messages 使用 add_messages reducer，支持 checkpointer 多轮对话累积历史。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    goal: InsightGoal | None
    plan: Literal["direct", "compose"]
    skill_calls: list[SkillCall]
    evidences: list[Evidence]
    insight: Insight | None
    final_response: str
    trace: AnswerTrace | None
    clarification: str | None
    # P1（D4）：复杂度判定。qa_router 写，conditional 路由消费（Task 2）
    complexity: Literal["light", "deep"] | None
    # P1（D4）：前端强制深度分析入口。ws.py 写，qa_router 读（仅未短路时生效）
    force_deep: bool | None
    # P1（D31）：deep 来源标记。escalate 写（合法 worker 名），Task 4 synth_answer 消费
    deep_source: Literal["stock", "sector", "hot_burst"] | None
    # P1（D24）：临时路由信号。escalate 回落 skill_executor 时置 True。
    # LangGraph 通道机制必需声明（节点返回未声明键会触发 InvalidUpdateError）；
    # 不进 trace/insight，conditional 边消费后即弃。
    fallback_to_skill: bool | None
