"""Supervisor Agent — 意图分类节点

使用 quick_think 模型进行意图识别，写入 state.intent。
不调用任何工具，纯 LLM 分类。
"""

from langchain_core.messages import SystemMessage

from aistock_agent.prompts.supervisor.routing import ROUTING_PROMPT
from aistock_agent.services.llm import get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.message import extract_last_human_message
from aistock_agent.utils.parser import parse_intent


async def run(state: AgentState) -> dict[str, object]:
    """意图分类：分析用户消息，写入 intent / symbol / tag_code"""
    llm = get_quick_think()

    # 取最后一条用户消息
    user_message = extract_last_human_message(state.get("messages", []))

    if not user_message:
        return {"intent": "general"}

    response = await llm.ainvoke(
        [
            SystemMessage(content=ROUTING_PROMPT),
            *state.get("messages", [])[-5:],  # 最近5条消息作为上下文
        ]
    )

    # 解析 LLM 输出为结构化意图
    # response.content 可能是 str 或 list[str | dict]（多模态内容），统一转 str
    content = response.content if isinstance(response.content, str) else str(response.content)
    result = parse_intent(content, user_message)
    return result
