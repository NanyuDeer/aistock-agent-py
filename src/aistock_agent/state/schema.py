"""AgentState — LangGraph 图的状态定义

所有数据通过 AgentState 流转，禁止节点间隐式传递。
"""

from typing import Annotated, NotRequired

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """LangGraph 全局状态

    Attributes:
        messages: 对话历史（add_messages reducer，追加不覆盖）
        session_id: 会话ID
        user_id: 用户ID（可选，未登录用户为 None）
        favorites: 用户自选股列表
        intent: 路由意图，由 supervisor 写入
        symbol: 提取的股票代码
        tag_code: 提取的板块代码
        analysis_reports: 多步分析报告累积（key=报告类型, value=内容）
        wind_leaders_data: 长线风口数据（Python入口预加载，wind_leader_agent读取）
        institution_research_data: 机构调研数据（Python入口预加载）
        trigger_source: 触发来源（scheduler=定时任务, user=用户对话）
        report_date: 报告日期（YYYY-MM-DD，scheduler 写入，Agent 持久化用）
        final_response: 最终响应文本
    """

    messages: Annotated[list[BaseMessage | dict[str, str]], add_messages]
    session_id: str
    user_id: str | None
    favorites: list[str]
    # 路由信息（supervisor 写入）
    # stock | sector | event | morning | wind_leader | broadcast | hot_burst | general
    intent: str | None
    symbol: str | None
    tag_code: str | None
    # 分析报告累积（broadcast_agent读取）
    analysis_reports: dict[str, object]
    # 预加载字段（Python入口写入，Agent读取）
    # 长线风口数据（通过工具按需加载，非入口预加载）
    wind_leaders_data: NotRequired[dict[str, object] | None]
    # 机构调研数据（通过工具按需加载，非入口预加载）
    institution_research_data: NotRequired[dict[str, object] | None]
    # 持久化控制（scheduler 写入，Agent 读取判断是否写数据库）
    trigger_source: NotRequired[str | None]  # "scheduler" | "user"
    report_date: NotRequired[str | None]  # YYYY-MM-DD
    brief_type: NotRequired[str | None]  # "morning" | "evening"
    trace_id: NotRequired[str | None]  # 个股或市场触发链路关联 ID
    # 最终响应
    final_response: str | None
