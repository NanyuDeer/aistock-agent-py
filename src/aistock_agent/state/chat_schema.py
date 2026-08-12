"""CHAT QA 链路独立状态。

不复用 AgentState，对齐 spec §2.3。
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    ChatCard,  # P11（线 3）：cards 卡片契约（B-T1 定义，与 P10 共享）
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
    # M1（2026-08-11）：澄清续跑 pending 上下文。qa_router 写澄清时快照原问题上下文，
    # 下一轮用户补全代码/名称时续跑原意图。跨轮有意（最长存活一轮：消费即清 /
    # 下轮未消费由 qa_router_node 包装层清空），明确不在 reset_transient_state 归零清单内。
    pending_clarification: dict | None
    # P11（线 3）/ P10（线 2）：cards 由 synth_answer 汇总写（线 3）；
    # token_usage 由 P10 包装函数 synth_answer_node 收口写（LLM callback 层经 contextvar 采集）。
    cards: list[ChatCard] | None
    token_usage: dict[str, int] | None
    # Phase 4-2（改进 13）：交互式确认负载（qa_router 触发写，synth_answer 短路透出，
    # ws.py 转 confirm_request 终态）。单轮 transient，不落 trace/insight。
    confirm: dict | None = None
    # Phase 4-2：阶段 2 续跑输入信号（ws.py 写，qa_router 消费）——用户点选的标的
    # （{"symbol": 6位代码, "label": 选项 label}）与确认超时标记。单轮 transient 输入，
    # 不写回图状态输出；由 ws.py 每轮入口归零（对齐 deep_source/goals 先例）。
    confirm_choice: dict | None = None
    confirm_timeout: bool | None = None
