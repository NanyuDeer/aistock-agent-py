"""General Agent — Chat 子图专用双模式入口（P7+P8 线 1）。

科普模式（D32）：单次 get_quick_think 轻量回答，不搜索、无工具——
  与既有固定话术相比提供动态、贴合用户问法的解释。
缺口模式（D37）：create_react_agent(quick_think, [tavily_finance_search])
  自由搜索，回答"能力型缺口"问题（如宏观影响、概念解释之外的自由提问）。

降级：两种模式均顶层 try-catch，失败返回规范降级文本，不抛异常中断图。
不破坏主图调用：本模块是独立入口，agents/general/node.py 不动。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.observability.logging import get_logger
from aistock_agent.services.llm import get_quick_think
from aistock_agent.tools.search_tools import tavily_finance_search
from aistock_agent.utils.message import extract_final_ai_response

logger = get_logger(__name__)

SCIENCE_PROMPT = (
    "你是股票投资知识科普助手。用通俗易懂的中文解释用户的问题（指标含义、"
    "市场机制、基础概念）。回答控制在 100-200 字，只讲事实，不给出买卖建议，"
    "不用 Markdown 列表。如果问题超出你的知识范围，如实说明。\n问题："
)

GAP_PROMPT = (
    "你是投资助手。用户的问题无法用现有行情/报告能力回答，请基于你的知识"
    "并结合 tavily_finance_search 搜索补充最新信息。回答控制在 200 字以内，"
    "区分事实与推断，标注信息时效，不给出买卖建议。"
)


async def run_science(question: str) -> str:
    """科普模式：单次 quick_think 动态回答。"""
    try:
        llm = get_quick_think()
        result = await llm.ainvoke([HumanMessage(content=SCIENCE_PROMPT + question)])
        text = getattr(result, "content", "") or ""
        return text.strip() or "该问题暂无法解释，请换个问法试试"
    except Exception as exc:  # 顶层兜底，不抛异常
        logger.warning("general_chat.science_failed", err=str(exc), exc_info=True)
        return "科普回答暂不可用，请稍后重试"


async def run_gap(question: str) -> str:
    """缺口模式：ReAct + tavily_finance_search 自由搜索。"""
    try:
        llm = get_quick_think()
        agent = create_react_agent(llm, tools=[tavily_finance_search])
        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=GAP_PROMPT),
                    HumanMessage(content=question),
                ]
            }
        )
        messages = result.get("messages", [])
        text = extract_final_ai_response(messages)
        if not text and messages:  # 兼容非 BaseMessage 消息对象（测试/扩展形态）
            text = getattr(messages[-1], "content", "") or ""
        return text.strip() or "该问题暂时无法解答，服务暂不可用，请稍后重试"
    except Exception as exc:  # 顶层兜底，不抛异常
        logger.warning("general_chat.gap_failed", err=str(exc), exc_info=True)
        return "该问题暂时无法解答，服务暂不可用，请稍后重试"
