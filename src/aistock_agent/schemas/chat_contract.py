"""CHAT QA 链路数据契约。

所有契约均为 Pydantic BaseModel + ConfigDict(extra="forbid")，对齐
docs/superpowers/specs/2026-07-28-chat-qa-mvp-design.md §3。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class InsightGoal(BaseModel):
    """用户问题目标契约，由 QA Router 产出。"""

    question: str
    symbols: list[str] = []
    tag_codes: list[str] = []
    time_range: Literal["realtime", "today", "recent", "history"] = "today"
    intent: Literal[
        "capital_flow",
        "evidence_resolver",
        "hot_burst",
        "industry_relation",
        "market_snapshot",
        "report_lookup",
        "sector_snapshot",
        "stock_news",
        "stock_snapshot",
        "trace_lookup",
    ]
    # QA Router 不填，由 synth_answer 通过 _infer_answer_mode 推断
    answer_mode: Literal["predict", "trace", "validate"] | None = None
    # constraints.answer_mode 可作为显式覆盖出口
    constraints: dict[str, str] = {}

    model_config = ConfigDict(extra="forbid")


class ChatSource(BaseModel):
    """CHAT 专用 Source 类型，不与 PROD 的 SourceRecord 双源。"""

    source_id: str
    kind: Literal["db_report", "realtime_quote", "news", "trace", "industry", "capital_flow"]
    title: str
    url: str | None = None
    snippet: str
    occurred_at: datetime | None = None
    captured_at: datetime

    model_config = ConfigDict(extra="forbid")


class Evidence(BaseModel):
    """由 Skills 产出，综合回答 Agent 消费。"""

    facts: list[str]
    sources: list[ChatSource]
    as_of: datetime
    symbols: list[str] = []
    degraded: bool = False
    degraded_reason: str | None = None
    skill_name: str
    raw: dict[str, Any] = {}

    model_config = ConfigDict(extra="forbid")


class Insight(BaseModel):
    """综合回答 Agent 产出的最终交付。"""

    conclusion: str
    basis: list[Evidence]
    confidence: Literal["high", "medium", "low"]
    uncertainty: list[str] = []
    answer_mode: Literal["predict", "trace", "validate", "deep"]  # D31：deep 统一出口

    model_config = ConfigDict(extra="forbid")


class SkillCall(BaseModel):
    """QA Router 产出的计划项。"""

    skill_name: Literal[
        "capital_flow",
        "evidence_resolver",
        "hot_burst",
        "industry_relation",
        "market_snapshot",
        "report_lookup",
        "sector_snapshot",
        "stock_news",
        "stock_snapshot",
        "trace_lookup",
    ]
    args: dict[str, Any]
    depends_on: list[str] = []

    model_config = ConfigDict(extra="forbid")


class AnswerTrace(BaseModel):
    """完整保存路由计划、证据、模式选择，供前端展示与调试。"""

    goal: InsightGoal
    plan: Literal["direct", "compose"]
    skill_calls: list[SkillCall]
    evidences: list[Evidence]
    actual_mode: Literal["predict", "trace", "validate", "deep"]  # D31：deep 统一出口

    model_config = ConfigDict(extra="forbid")
