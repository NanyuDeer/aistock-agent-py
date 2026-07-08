"""General Agent — 兜底节点

模型：quick_think
工具集：get_quote（基础行情）
"""

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.general.system import GENERAL_PROMPT
from aistock_agent.services.llm import get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.stock_tools import get_quote
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """兜底对话：关键词触发基础查询"""
    try:
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

        final_response = extract_final_ai_response(result.get("messages", []))

        return {"final_response": final_response}
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="general",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "抱歉，我暂时无法处理您的请求，请稍后重试"}
