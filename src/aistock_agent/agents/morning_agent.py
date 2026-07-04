"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时
"""

from collections.abc import AsyncGenerator
from datetime import date, datetime

import redis.asyncio as aioredis
from chinese_calendar import is_workday
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.agents.base import get_deep_think
from aistock_agent.config import settings
from aistock_agent.prompts.morning import MORNING_PROMPT
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search
from aistock_agent.tools.news_tools import get_cls_news

TOOL_LABELS: dict[str, str] = {
    "get_global_markets":    "正在获取全球市场行情",
    "tavily_finance_search": "正在搜索财经新闻",
    "get_cls_news":          "正在获取财联社资讯",
}


async def stream(state: dict) -> AsyncGenerator[dict, None]:
    """晨报 SSE 流：缓存命中直接返回，未命中走 ReAct + astream_events"""
    today = datetime.now().strftime("%Y年%m月%d日")

    cached = await _get_cached_briefing()
    if cached:
        yield {"type": "text", "content": cached}
        yield {"type": "done"}
        return

    system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
    if not is_trading_day():
        system_prompt += (
            "\n\n注意：今日为非交易日（周末或节假日），"
            "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
        )

    llm = get_deep_think()
    tools = [tavily_finance_search, get_global_markets, get_cls_news]
    react_agent = create_react_agent(llm, tools)

    _llm_started = False
    _response_parts: list[str] = []

    try:
        async for event in react_agent.astream_events(
            {"messages": [SystemMessage(content=system_prompt)]},
            version="v2",
        ):
            event_type = event.get("event")
            tool_name = event.get("name", "")

            if event_type == "on_tool_start":
                label = TOOL_LABELS.get(tool_name, tool_name)
                tool_event: dict = {
                    "type": "tool_start",
                    "tool": tool_name,
                    "label": label,
                }
                query = event.get("data", {}).get("input", {}).get("query")
                if query:
                    tool_event["args"] = {"query": query}
                yield tool_event

            elif event_type == "on_tool_end":
                yield {"type": "tool_end", "tool": tool_name}

            elif event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk:
                    has_text = bool(chunk.content)
                    has_tool_calls = bool(
                        getattr(chunk, "tool_calls", None)
                        or getattr(chunk, "tool_call_chunks", None)
                    )
                    if has_text and not has_tool_calls:
                        if not _llm_started:
                            _llm_started = True
                            yield {"type": "llm_start", "label": "正在生成分析报告"}
                        yield {"type": "text", "content": chunk.content}
                        _response_parts.append(chunk.content)

        final_response = "".join(_response_parts)
        if final_response:
            await _set_cached_briefing(final_response)

    except Exception as e:
        yield {"type": "error", "message": str(e)}
        return

    yield {"type": "done"}


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
        client = aioredis.from_url(settings.redis_url)
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        await client.setex(cache_key, ttl, content)
        await client.aclose()
    except Exception:
        pass


def is_trading_day(d: date | None = None) -> bool:
    """判断是否为 A 股交易日（排除周末和法定节假日）"""
    return is_workday(d or date.today())
