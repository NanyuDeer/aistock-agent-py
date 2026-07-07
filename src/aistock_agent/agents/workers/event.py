"""Event Analyst Agent — 事件传导链分析

工具集：search_cls_news, get_news_fulltext, get_quote, tavily_finance_search
"""

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.event import EVENT_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.market_tools import tavily_finance_search
from aistock_agent.tools.news_tools import get_news_fulltext, search_cls_news
from aistock_agent.tools.stock_tools import get_quote
from aistock_agent.utils.message import extract_final_ai_response


async def run(state: AgentState) -> dict[str, object]:
    """事件传导链分析：事件→行业→个股"""
    llm = get_deep_think()
    tools = [search_cls_news, get_news_fulltext, get_quote, tavily_finance_search]
    agent = create_react_agent(llm, tools)

    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=EVENT_ANALYST_PROMPT),
                *state.get("messages", [])[-5:],
            ]
        }
    )

    final_response = extract_final_ai_response(result.get("messages", []))

    return {"final_response": final_response}
