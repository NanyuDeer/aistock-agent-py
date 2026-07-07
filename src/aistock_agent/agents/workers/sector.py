"""Sector Analyst Agent — 板块分析

工具集：get_leader_stocks, get_capital_flow
"""

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.sector import SECTOR_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.sector_tools import get_leader_stocks
from aistock_agent.tools.stock_tools import get_capital_flow


async def run(state: AgentState) -> dict[str, object]:
    """板块分析：龙头筛选 + 资金动向"""
    tag_code = state.get("tag_code") or "BK0475"  # 默认白酒板块

    llm = get_deep_think()
    tools = [get_leader_stocks, get_capital_flow]
    agent = create_react_agent(llm, tools)

    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=SECTOR_ANALYST_PROMPT),
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
