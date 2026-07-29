"""CHAT QA 链路独立状态。

不复用 AgentState，对齐 spec §2.3。
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage

from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    Evidence,
    Insight,
    InsightGoal,
    SkillCall,
)


class QuestionState(TypedDict, total=False):
    """CHAT QA 链路状态。"""

    messages: list[BaseMessage]
    goal: InsightGoal | None
    plan: Literal["direct", "compose"]
    skill_calls: list[SkillCall]
    evidences: list[Evidence]
    insight: Insight | None
    final_response: str
    trace: AnswerTrace | None
