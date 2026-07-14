"""事件传导链分析 Agent — v3 模块化版本

将事件分析拆分为 5 个独立 LLM 调用，通过 transform_to_frontend 对齐前端格式。

调用流程：
1. Understanding (flash, no tools)      → 事件理解
2. Transmission (deep, ReAct + tools)    → 传导路径
3. History     (flash, ReAct + tools)    → 历史复盘
4. Investment  (flash, no tools)         → 投资建议
5. Podcast     (flash, no tools)         → 播报摘要
"""

import hashlib
import json
from typing import Literal, cast, overload

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.event import (
    EVENT_HISTORY_PROMPT,
    EVENT_INVESTMENT_PROMPT,
    EVENT_PODCAST_PROMPT,
    EVENT_TRANSMISSION_PROMPT,
    EVENT_UNDERSTANDING_PROMPT,
)
from aistock_agent.services.cache import get_cached_event, set_cached_event
from aistock_agent.services.event_persister import persist_event_report
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.output_parser import _parse_json, transform_to_frontend

logger = structlog.get_logger()

# 前端对齐的工具集名称
_TOOL_GROUP = "event"


# ── 内部辅助函数 ──


@overload
async def _call_llm_no_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
    *,
    raw_text: Literal[True],
) -> str | None: ...


@overload
async def _call_llm_no_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
    *,
    raw_text: Literal[False] = False,
) -> dict[str, object] | None: ...


async def _call_llm_no_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
    *,
    raw_text: bool = False,
) -> dict[str, object] | str | None:
    """调用 LLM（不带工具），返回解析后的 dict、原始文本或 None。

    使用 flash 模型做快速理解/投资/播报；deep 模型仅在需要时使用。
    raw_text=True 时跳过 JSON 解析，直接返回原始文本（供播报等非 JSON 场景）。
    """
    try:
        llm = get_deep_think() if model == "deep" else get_quick_think()
        result = await llm.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg),
            ]
        )
        text = cast(str, result.content) if hasattr(result, "content") else str(result)
        if raw_text:
            return text
        parsed = _parse_json(text)
        if isinstance(parsed, dict):
            return parsed
        logger.warning("llm_no_tools_not_dict", text_preview=text[:200])
        return None
    except Exception:
        logger.exception("llm_call_no_tools_failed", model=model)
        return None


async def _call_llm_with_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
) -> dict[str, object] | list[object] | None:
    """调用 ReAct agent（带工具），返回解析后的 dict/list 或 None。

    Transmission 使用 deep 模型；History 使用 flash 模型。
    """
    try:
        llm = get_deep_think() if model == "deep" else get_quick_think()
        tools = get_tools(_TOOL_GROUP)
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_msg),
                ]
            }
        )
        # ReAct agent 返回 messages 列表，取最后一条 AI 消息
        text = ""
        for msg in reversed(result.get("messages", [])):
            if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content.strip():
                text = msg.content
                break
        parsed = _parse_json(text)
        return parsed  # dict | list | None
    except Exception:
        logger.exception("llm_call_with_tools_failed", model=model)
        return None


async def _analyze_understanding(user_msg: str) -> dict[str, object] | None:
    """Call 1: 事件理解（flash 模型，无工具）。"""
    return await _call_llm_no_tools(EVENT_UNDERSTANDING_PROMPT, user_msg, model="flash")


async def _analyze_transmission(
    user_msg: str, understanding: dict[str, object]
) -> dict[str, object] | None:
    """Call 2: 传导路径分析（deep 模型，ReAct + 工具）。"""
    ud = json.dumps(understanding, ensure_ascii=False)
    prompt = EVENT_TRANSMISSION_PROMPT.replace("{understanding}", ud)
    result = await _call_llm_with_tools(prompt, user_msg, model="deep")
    if isinstance(result, dict):
        return result
    logger.warning("transmission_not_dict")
    return None


async def _analyze_history(
    user_msg: str, understanding: dict[str, object]
) -> list[object] | None:
    """Call 3: 历史复盘（flash 模型，ReAct + 工具）。"""
    ud = json.dumps(understanding, ensure_ascii=False)
    prompt = EVENT_HISTORY_PROMPT.replace("{understanding}", ud)
    result = await _call_llm_with_tools(prompt, user_msg, model="flash")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        # LLM 偶尔返回单对象而非数组，包装为 list
        return [result]
    logger.warning("history_not_list")
    return None


async def _analyze_investment(
    understanding: dict[str, object] | None,
    transmission: dict[str, object] | None,
    history: list[object] | None,
) -> dict[str, object] | None:
    """Call 4: 投资建议（flash 模型，无工具，注入前 3 步结果）。

    使用 str.replace 注入上下文而非 str.format，
    因为 prompt 中包含 JSON 示例的花括号会导致 format 崩溃。
    """
    ud = json.dumps(understanding, ensure_ascii=False) if understanding else "无"
    td = json.dumps(transmission, ensure_ascii=False) if transmission else "无"
    hd = json.dumps(history, ensure_ascii=False) if history else "无"
    prompt = (
        EVENT_INVESTMENT_PROMPT
        .replace("{understanding}", ud)
        .replace("{transmission}", td)
        .replace("{history}", hd)
    )
    return await _call_llm_no_tools(prompt, "综合上述分析生成投资观点", model="flash")


async def _generate_podcast(
    understanding: dict[str, object] | None, conclusion: str
) -> str:
    """Call 5: 播报摘要（flash 模型，无工具，注入理解摘要 + 投资结论）。

    使用 str.replace 注入上下文而非 str.format，
    因为 prompt 中包含 JSON 示例的花括号会导致 format 崩溃。
    """
    summary = str(understanding.get("summary", "")) if understanding else ""
    prompt = (
        EVENT_PODCAST_PROMPT
        .replace("{understanding_summary}", summary)
        .replace("{conclusion}", conclusion)
    )
    text = await _call_llm_no_tools(
        prompt, "生成播报摘要", model="flash", raw_text=True
    )
    if isinstance(text, str) and text.strip():
        return text.strip()
    logger.warning("podcast_generation_failed")
    return "事件播报生成失败，请稍后重试"


# ── 主入口 ──


async def run(state: AgentState) -> dict[str, object]:
    """事件分析 Agent 主入口。

    5 个 LLM 调用 → transform_to_frontend → 返回 analysis_reports。
    """
    messages = state.get("messages", [])
    if not messages:
        return {"final_response": "请提供需要分析的事件描述。", "analysis_reports": {}}

    # 提取用户消息文本
    user_msg = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) and isinstance(msg.content, str) and msg.content.strip():
            user_msg = msg.content.strip()
            break
    if not user_msg:
        return {"final_response": "请提供需要分析的事件描述。", "analysis_reports": {}}

    try:
        # 缓存检查
        cached = await get_cached_event(user_msg)
        if cached:
            logger.info("event_cache_hit", event_preview=user_msg[:50])
            # 缓存存储的是完整 analysis_reports dict（与 transform_to_frontend 输出一致），
            # 直接返回，保证缓存命中/未命中时前端收到相同的数据结构。
            podcast_brief = str(cached.get("event_podcast_brief", ""))
            return {
                "final_response": podcast_brief,
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    **cached,
                },
            }

        # ── 5 个独立 LLM 调用 ──

        # Call 1: 事件理解（flash, no tools）
        understanding = await _analyze_understanding(user_msg)
        if not understanding:
            logger.warning("event_understanding_failed", event_preview=user_msg[:50])
            return {
                "final_response": "事件分析暂时不可用，请稍后重试",
                "analysis_reports": {},
            }

        # Call 2: 传导路径（deep, ReAct + tools）
        transmission = await _analyze_transmission(user_msg, understanding)

        # Call 3: 历史复盘（flash, ReAct + tools）
        history = await _analyze_history(user_msg, understanding)

        # Call 4: 投资建议（flash, no tools, 注入前 3 步结果）
        investment = await _analyze_investment(understanding, transmission, history)

        # Call 5: 播报摘要（flash, no tools, 注入理解摘要 + 投资结论）
        conclusion = ""
        if investment and isinstance(investment, dict):
            conclusion = str(investment.get("conclusion", ""))
        podcast_brief = await _generate_podcast(understanding, conclusion)

        # ── 构建前端对齐的 analysis_reports ──
        event_meta: dict[str, object] = {
            "eventId": f"evt_{hashlib.md5(user_msg.encode()).hexdigest()[:8]}",
            "title": user_msg[:50],
            "source": "",
        }
        analysis_reports = transform_to_frontend(
            understanding,
            transmission,
            history,
            investment,
            event_meta,
        )
        analysis_reports["event_podcast_brief"] = podcast_brief

        # 缓存 & 持久化
        # 缓存存储完整 analysis_reports（含 event_understanding/transmission/history/
        # investment/podcast_brief），保证缓存命中时前端数据结构与新鲜执行一致。
        await set_cached_event(user_msg, analysis_reports)
        event_id = str(event_meta.get("eventId", ""))
        await persist_event_report(event_id, event_meta, user_msg, analysis_reports)

        return {
            "final_response": podcast_brief,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                **analysis_reports,
            },
        }
    except Exception:
        logger.exception("agent_run_failed", agent="event_analyst_v3")
        return {
            "final_response": "事件分析暂时不可用，请稍后重试",
            "analysis_reports": {},
        }
