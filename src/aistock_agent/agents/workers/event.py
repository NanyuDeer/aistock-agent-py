"""Event Analyst Agent — 事件传导链分析（v2 升级）

工具集：search_cls_news, get_news_fulltext, get_quote, tavily_finance_search, match_industry_by_keywords

v2 升级内容：
- Redis 缓存（避免同一事件 30 分钟内重复分析）
- 双层输出解析（display_report + podcast_brief）
- 持久化到 Node.js（analysis_reports 表）
"""

from datetime import datetime
import hashlib
import json

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.event import EVENT_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.services.data_client import node_api
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.output_parser import parse_event_output

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """事件传导链分析：v2 — 缓存 → LLM → 解析 → 持久化"""
    try:
        # 获取用户输入（最后一条用户消息）
        user_msg = _get_last_user_message(state.get("messages", []))
        if not user_msg:
            return {"final_response": "请提供需要分析的事件描述。", "analysis_reports": {}}

        # Step 1: Redis 缓存检查（基于事件内容 MD5，30 分钟）
        cached = await _check_cache(user_msg)
        if cached:
            logger.info("event_analysis_cache_hit", event_preview=user_msg[:50])
            return {
                "final_response": cached["podcast_brief"],
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "event_display_report": cached["display_report"],
                    "event_podcast_brief": cached["podcast_brief"],
                },
            }

        # Step 2: LLM 调用（ReAct agent + event 工具集）
        llm = get_deep_think()
        tools = get_tools("event")
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=EVENT_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        # Step 3: 解析双层输出
        display_report, podcast_brief = parse_event_output(result.get("messages", []))

        if not podcast_brief:
            # 降级：LLM 输出格式不符合预期，回退到原始文本
            from aistock_agent.utils.message import extract_final_ai_response
            fallback = extract_final_ai_response(result.get("messages", []))
            logger.warning("event_parse_fallback", preview=fallback[:100] if fallback else "")
            return {"final_response": fallback or "事件分析结果格式异常，请稍后重试", "analysis_reports": {}}

        # Step 4: Redis 缓存（TTL 30 分钟）
        await _set_cache(user_msg, display_report, podcast_brief)

        # Step 5: 持久化到 Node.js（同步调用，非关键路径，失败不影响返回）
        await _persist_event_report(user_msg, display_report, podcast_brief)

        return {
            "final_response": podcast_brief,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "event_display_report": display_report,
                "event_podcast_brief": podcast_brief,
            },
        }

    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="event_analyst_v2",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "事件分析暂时不可用，请稍后重试", "analysis_reports": {}}


# ==================== Helper Functions ====================

def _get_last_user_message(messages: list[object]) -> str:
    """提取最后一条用户消息"""
    from aistock_agent.utils.message import extract_last_human_message
    return extract_last_human_message(messages) if messages else ""


def _cache_key(user_input: str) -> str:
    """生成缓存 key：event:{md5}"""
    digest = hashlib.md5(user_input.encode()).hexdigest()
    return f"event:{digest}"


async def _check_cache(user_input: str) -> dict[str, object] | None:
    """检查 Redis 缓存"""
    try:
        client = await RedisPool.get_client()
        key = _cache_key(user_input)
        cached = await client.get(key)
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
    except Exception:
        logger.debug("event_cache_check_failed", exc_info=True)
    return None


async def _set_cache(
    user_input: str,
    display_report: dict[str, object] | None,
    podcast_brief: str,
    ttl: int = 1800,
) -> None:
    """写入 Redis 缓存（TTL 默认 30 分钟）"""
    try:
        client = await RedisPool.get_client()
        key = _cache_key(user_input)
        value = json.dumps({
            "display_report": display_report,
            "podcast_brief": podcast_brief,
        }, ensure_ascii=False)
        await client.setex(key, ttl, value)
    except Exception:
        logger.debug("event_cache_set_failed", exc_info=True)


async def _persist_event_report(
    event: str,
    display_report: dict[str, object] | None,
    podcast_brief: str,
) -> None:
    """持久化事件分析报告到 Node.js /internal/analysis-reports（非关键路径）"""
    report_date = datetime.now().strftime("%Y-%m-%d")

    try:
        await node_api.post("/internal/analysis-reports", {
            "report_type": "event_conduction",
            "report_date": report_date,
            "user_id": "system",
            "content": {
                "event": event[:500],
                "display_report": display_report,
                "podcast_brief": podcast_brief,
            },
            "data_source": "event_agent_v2",
            "status": "completed",
        })
        logger.info("event_report_persisted", date=report_date)
    except Exception:
        logger.debug("event_report_persist_failed", exc_info=True)
