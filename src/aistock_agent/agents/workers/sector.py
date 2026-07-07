"""Sector Analyst Agent — 板块分析

工具集：get_leader_stocks, get_capital_flow
"""

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.sector import SECTOR_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.sector_tools import get_leader_stocks
from aistock_agent.tools.stock_tools import get_capital_flow
from aistock_agent.utils.message import extract_final_ai_response


async def run(state: AgentState) -> dict[str, object]:
    """板块分析：龙头筛选 + 资金动向"""
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

    final_response = extract_final_ai_response(result.get("messages", []))

    return {"final_response": final_response}
