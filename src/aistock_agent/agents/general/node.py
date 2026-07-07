"""General Agent — 兜底节点

模型：quick_think
工具集：get_quote（基础行情）
"""

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.general.system import GENERAL_PROMPT
from aistock_agent.services.llm import get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.stock_tools import get_quote


async def run(state: AgentState) -> dict[str, object]:
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
        if isinstance(msg, BaseMessage) and msg.type == "ai" and msg.content:
            final_response = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    return {"final_response": final_response}
