"""Supervisor Agent — 意图分类节点

使用 quick_think 模型进行意图识别，写入 state.intent。
不调用任何工具，纯 LLM 分类。
"""

from langchain_core.messages import SystemMessage

from aistock_agent.agents.base import get_quick_think
from aistock_agent.prompts.routing import ROUTING_PROMPT
from aistock_agent.state.schema import AgentState


async def run(state: AgentState) -> dict:
    """意图分类：分析用户消息，写入 intent / symbol / tag_code"""
    llm = get_quick_think()

    # 取最后一条用户消息
    user_message = ""
    for msg in reversed(state.get("messages", [])):
        if hasattr(msg, "type") and msg.type == "human":
            user_message = msg.content
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
    result = _parse_intent(response.content, user_message)
    return result


def _parse_intent(llm_output: str, user_message: str) -> dict:
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
