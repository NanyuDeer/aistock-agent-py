"""CHAT QA 链路独立状态。

不复用 AgentState，对齐 spec §2.3。
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    ChatCard,
    Evidence,
    Insight,
    InsightGoal,
    SkillCall,
    SubGoal,
)


class DeepReportRef(TypedDict, total=False):
    """D12/D13：最近一次深度升级的引用 + 短摘要（单引用，不存全文）。

    worker 由 escalate 写入的 deep_source 保证合法（stock/sector/hot_burst）。
    report_id 为 chat_analysis 落库后 Node 返回的 id；落库失败/未登录为 None（D38）。
    summary ≤200 字（D18 约定 final_response 前 160 字截取）。
    """

    worker: Literal["stock", "sector", "hot_burst"]
    report_id: str | None
    question: str
    summary: str
    symbols: list[str]
    tag_codes: list[str]
    created_at: str


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
    # P2（D11）：前端透传的用户身份。ws.py 写；chat_analysis 落库登录守卫（T3）与
    # 追问复用 report_lookup（T5）消费；未登录为 None（D38 不落库）。
    user_id: str | None
    # P2（D12/D13/D39）：最近一次深度升级的引用（synth_answer deep 分支无条件写，
    # 与登录无关）；供 T5 qa_router 追问注入摘要；更早的靠 messages 历史兜底。
    last_deep_report: DeepReportRef | None
    # P4（D34）：多子目标（多意图 compose 时非空；存量会话/单意图为 None）。
    # 单轮 transient 路由信号，ws.py/routes.py 入口按轮置 None（对齐 deep_source）。
    goals: list[SubGoal] | None
    # P7+P8（D37/D32）：general 兜底来源标记。qa_router 写，conditional 路由消费。
    # 单轮 transient 路由信号，ws.py/routes.py 入口按轮置 None（对齐 deep_source/goals 先例）。
    general_source: Literal["science", "gap"] | None
    # P10 线 2（扩展选项 A）：token 用量快照 + 卡片汇总（cards 本阶段恒 None，
    # 计划 C 才产出）。synth_answer 包装写入，ws.py/routes.py DONE 附带。
    # 单轮 transient：ws.py/routes.py 入口 reset 采集层，state 字段每轮由 synth_answer 重写。
    token_usage: dict[str, int] | None
    cards: list[ChatCard] | None
