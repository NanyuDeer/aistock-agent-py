"""General Agent — 兜底节点

模型：quick_think
工具集：get_quote（基础行情）
"""

from typing import Any

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.agents.base import get_quick_think
from aistock_agent.prompts.system import GENERAL_PROMPT
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.stock_tools import get_quote


async def run(state: AgentState) -> dict[str, Any]:
    """兜底对话：关键词触发基础查询"""
    llm = get_quick_think()
    tools = [get_quote]
    agent = create_react_agent(llm, tools)

    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=GENERAL_PROMPT),
                *state.get("messages", [])[-5:],
            ]
        }
    )

    final_response = ""
    for msg in reversed(result.get("messages", [])):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            final_response = msg.content
            break

    return {"final_response": final_response}
