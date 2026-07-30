"""CHAT QA 链路独立状态。

不复用 AgentState，对齐 spec §2.3。
"""
from __future__ import annotations

from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from typing_extensions import Annotated

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
