"""AgentState — LangGraph 图的状态定义

所有数据通过 AgentState 流转，禁止节点间隐式传递。
"""

from typing import Annotated

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
        final_response: 最终响应文本
    """

    messages: Annotated[list[BaseMessage | dict[str, str]], add_messages]
    session_id: str
    user_id: str | None
    favorites: list[str]
    # 路由信息（supervisor 写入）
    intent: str | None  # stock | sector | event | morning | general
    symbol: str | None
    tag_code: str | None
    # 分析报告累积
    analysis_reports: dict[str, str]
    # 最终响应
    final_response: str | None
