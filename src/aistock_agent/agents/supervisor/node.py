"""Supervisor Agent — 意图分类节点

使用 quick_think 模型进行意图识别，写入 state.intent。
不调用任何工具，纯 LLM 分类。
"""

from langchain_core.messages import BaseMessage, SystemMessage

from aistock_agent.prompts.supervisor.routing import ROUTING_PROMPT
from aistock_agent.services.llm import get_quick_think
from aistock_agent.state.schema import AgentState


async def run(state: AgentState) -> dict[str, object]:
    """意图分类：分析用户消息，写入 intent / symbol / tag_code"""
    llm = get_quick_think()

    # 取最后一条用户消息
    user_message = ""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, BaseMessage) and msg.type == "human":
            user_message = msg.content if isinstance(msg.content, str) else str(msg.content)
            break
        if isinstance(msg, dict) and msg.get("role") == "user":
            user_message = msg.get("content", "")
            break

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
    result = _parse_intent(content, user_message)
    return result


def _parse_intent(llm_output: str, user_message: str) -> dict[str, object]:
    """解析 LLM 分类输出为 state 字段"""
    output = llm_output.strip().lower()

    intent = "general"
    symbol = None
    tag_code = None

    # 从 LLM 输出解析意图
    if "morning" in output:
        intent = "morning"
    elif "event" in output:
        intent = "event"
    elif "sector" in output:
        intent = "sector"
    elif "stock" in output:
        intent = "stock"

    # 从原始消息提取股票代码和板块代码
    import re

    symbol_match = re.search(r"\b(\d{6})\b", user_message)
    if symbol_match:
        symbol = symbol_match.group(1)

    tag_match = re.search(r"BK\d+", user_message, re.IGNORECASE)
    if tag_match:
        tag_code = tag_match.group(0).upper()

    return {"intent": intent, "symbol": symbol, "tag_code": tag_code}
