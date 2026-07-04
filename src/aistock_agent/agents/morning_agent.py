"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时
"""

import json
from datetime import datetime

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.agents.base import get_deep_think
from aistock_agent.config import settings
from aistock_agent.prompts.morning import MORNING_PROMPT
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search
from aistock_agent.tools.news_tools import get_cls_news


async def run(state: AgentState) -> dict:
    """晨报分析：宏观策略4步框架"""
    today = datetime.now().strftime("%Y年%m月%d日")

    # 检查缓存
    cached = await _get_cached_briefing()
    if cached:
        return {"final_response": cached}

    # 构建提示词
    system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)

    # 创建 ReAct Agent
    llm = get_deep_think()
    tools = [tavily_finance_search, get_global_markets, get_cls_news]
    agent = create_react_agent(llm, tools)

    # 执行
    result = await agent.ainvoke(
        {"messages": [SystemMessage(content=system_prompt)]},
    )

    # 提取最终响应
    final_response = ""
    for msg in reversed(result.get("messages", [])):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            final_response = msg.content
            break

    # 缓存结果
    if final_response:
        await _set_cached_briefing(final_response)

    return {"final_response": final_response}


async def _get_cached_briefing() -> str | None:
    """从 Redis 获取缓存晨报"""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url)
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        cached = await client.get(cache_key)
        await client.aclose()
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        pass
    return None


async def _set_cached_briefing(content: str, ttl: int = 7200) -> None:
    """缓存晨报到 Redis，TTL=2小时"""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url)
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        await client.setex(cache_key, ttl, content)
        await client.aclose()
    except Exception:
        pass
