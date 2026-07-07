"""Stock Analyst Agent — 个股综合分析

工具集：get_quote, get_capital_flow, get_profit_forecast, search_cls_news
"""

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.stock import STOCK_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.news_tools import search_cls_news
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """个股分析：行情 + 资金流向 + 机构预测 + 相关新闻"""
    symbol = state.get("symbol")
    if not symbol:
        return {"final_response": "请提供股票代码，例如：分析一下 600519"}

    try:
        llm = get_deep_think()
        tools = [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=STOCK_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        return {"final_response": final_response}
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="stock_analyst",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "个股分析暂时不可用，请稍后重试"}
