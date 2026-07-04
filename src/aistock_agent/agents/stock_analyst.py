"""Stock Analyst Agent — 个股综合分析

工具集：get_quote, get_capital_flow, get_profit_forecast, search_cls_news
"""

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.agents.base import get_deep_think
from aistock_agent.prompts.system import STOCK_ANALYST_PROMPT
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.news_tools import search_cls_news
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote


async def run(state: AgentState) -> dict:
    """个股分析：行情 + 资金流向 + 机构预测 + 相关新闻"""
    symbol = state.get("symbol")
    if not symbol:
        return {"final_response": "请提供股票代码，例如：分析一下 600519"}

    llm = get_deep_think()
    tools = [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]
    agent = create_react_agent(llm, tools)

    # 取用户消息
    user_message = ""
    for msg in reversed(state.get("messages", [])):
        if hasattr(msg, "type") and msg.type == "human":
            user_message = msg.content
            break

    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=STOCK_ANALYST_PROMPT),
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
