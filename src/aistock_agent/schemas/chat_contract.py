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
        "compare_stocks",
        "stock_history",
        "trend_ranking",
        "index_snapshot",
    ]
    # QA Router 不填，由 synth_answer 通过 _infer_answer_mode 推断
    answer_mode: Literal["predict", "trace", "validate"] | None = None
    # constraints.answer_mode 可作为显式覆盖出口
    constraints: dict[str, str] = {}

    model_config = ConfigDict(extra="forbid")


class SubGoal(BaseModel):
    """D34 多子目标：组合问题的独立子目标。

    id 是 skill_calls/evidence 归属引用的锚点（LLM 输出 g1..gN，
    默认 g1，后处理层校验唯一性并按列表顺序重编号，见 qa_router §5.1）。
    dimension 即 D30 业务维度，predict 子目标由 synth_answer 输出 D35 降级提示。
    """

    id: str = "g1"
    question: str
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
        "compare_stocks",
        "stock_history",
        "trend_ranking",
        "index_snapshot",
    ]
    dimension: Literal["predict", "trace", "validate"]
    symbols: list[str] = []
    tag_codes: list[str] = []
    time_range: Literal["realtime", "today", "recent", "history"] = "today"

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
    # D34：skill_executor 从 SkillCall 透传；单意图恒为 None
    goal_id: str | None = None

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
        "compare_stocks",
        "stock_history",
        "trend_ranking",
        "index_snapshot",
    ]
    args: dict[str, Any]
    depends_on: list[str] = []
    # D34：归属子目标（goals 非空时引用 g1..gN；单意图恒为 None）
    goal_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class AnswerTrace(BaseModel):
    """完整保存路由计划、证据、模式选择，供前端展示与调试。"""

    goal: InsightGoal
    plan: Literal["direct", "compose"]
    skill_calls: list[SkillCall]
    evidences: list[Evidence]
    actual_mode: Literal["predict", "trace", "validate", "deep"]  # D31：deep 统一出口
    # D34：多子目标（透出调试/前端 trace 兼容；存量 None）
    goals: list[SubGoal] | None = None

    model_config = ConfigDict(extra="forbid")


class ChatCard(BaseModel):
    """P10 线 2/P11：DONE 负载卡片契约（本阶段 cards 恒 None，计划 C 才产出数据）。

    card_type 5 类高价值卡片（P11 spec §2.1）；data 为各类型结构化 payload。
    本计划只定义契约，不产出数据；计划 C 只消费不修改本类。
    """

    card_type: Literal[
        "market_snapshot",
        "stock_snapshot",
        "capital_flow",
        "deep",
        "comparison",
    ]
    title: str
    data: dict[str, Any]

    model_config = ConfigDict(extra="forbid")
