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
from dataclasses import dataclass
from typing import Literal, cast, overload

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.event import (
    EVENT_HISTORY_PROMPT,
    EVENT_INVESTMENT_PROMPT,
    EVENT_PODCAST_PROMPT,
    EVENT_TRANSMISSION_PROMPT,
    EVENT_UNDERSTANDING_PROMPT,
)
from aistock_agent.services.cache import get_cached_event, set_cached_event
from aistock_agent.services.event_graph_resolver import resolve_industry_graph_evidence
from aistock_agent.services.event_persister import persist_event_report
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_last_human_message
from aistock_agent.utils.output_parser import _parse_json, transform_to_frontend

logger = structlog.get_logger()

# 前端对齐的工具集名称
_TOOL_GROUP = "event"
_INDUSTRY_GRAPH_MISSING_BOUNDARY = "本次未取得 IndustryKG 图谱事实，上下游关系未展开，不能补造。"
_INDUSTRY_GRAPH_DEGRADED_STATUSES = {
    "invalid_input",
    "not_found",
    "authentication_failed",
    "upstream_failed",
    "timeout",
    "request_failed",
    "invalid_response",
}
_INDUSTRY_GRAPH_BOUNDARY_VERSION = "one_hop_v1"


@dataclass(frozen=True)
class _ToolCallResult:
    """ReAct 最终文本及 Transmission 可审计工具证据。"""

    parsed: dict[str, object] | list[object] | None
    industry_graph_evidence: list[dict[str, object]]


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


def _extract_industry_graph_evidence(messages: object) -> list[dict[str, object]]:
    """只从 IndustryKG 工具消息提取结构化图谱证据。"""
    if not isinstance(messages, list):
        return []

    evidence: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name != "get_industry_chain":
            continue
        if not isinstance(message.content, str):
            evidence.append(_invalid_industry_graph_evidence())
            continue
        try:
            parsed = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            evidence.append(_invalid_industry_graph_evidence())
            continue
        if isinstance(parsed, dict) and _is_valid_industry_graph_evidence(parsed):
            evidence.append(parsed)
        else:
            evidence.append(_invalid_industry_graph_evidence())
    return evidence


def _is_valid_industry_graph_evidence(evidence: dict[str, object]) -> bool:
    """校验 Transmission 可消费的 IndustryKG 一跳证据骨架。"""
    status = evidence.get("status")
    missing_boundary = evidence.get("missingBoundary")
    if not isinstance(status, str) or evidence.get("scope") != "one_hop":
        return False

    if status == "found":
        industry = evidence.get("industry")
        upstream = evidence.get("upstream")
        downstream = evidence.get("downstream")
        if (
            evidence.get("degraded") is not False
            or evidence.get("source") != "IndustryKGService"
            or not isinstance(upstream, list)
            or not isinstance(downstream, list)
        ):
            return False
        return (
            _is_valid_industry_node(industry)
            and all(
                _is_valid_industry_node(node, requires_leading_stocks=True)
                for node in upstream
            )
            and all(
                _is_valid_industry_node(node, requires_leading_stocks=True)
                for node in downstream
            )
        )

    return (
        status in _INDUSTRY_GRAPH_DEGRADED_STATUSES
        and evidence.get("degraded") is True
        and evidence.get("source") is None
        and evidence.get("industry") is None
        and evidence.get("upstream") is None
        and evidence.get("downstream") is None
        and isinstance(missing_boundary, str)
        and bool(missing_boundary.strip())
    )


def _is_valid_industry_node(value: object, *, requires_leading_stocks: bool = False) -> bool:
    """校验 IndustryKG 行业节点的最小身份字段。"""
    if not isinstance(value, dict):
        return False
    industry_id = value.get("id")
    name = value.get("name")
    if not (
        isinstance(industry_id, str)
        and industry_id.strip()
        and isinstance(name, str)
        and name.strip()
    ):
        return False
    return not requires_leading_stocks or isinstance(value.get("leadingStocks"), list)


def _not_queried_industry_graph_evidence() -> dict[str, object]:
    """在 Transmission 未调用图谱工具时标明事实边界。"""
    return {
        "status": "not_queried",
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": _INDUSTRY_GRAPH_MISSING_BOUNDARY,
    }


def _invalid_industry_graph_evidence() -> dict[str, object]:
    """在已调用图谱工具但响应不可解析时标明无效响应边界。"""
    return {**_not_queried_industry_graph_evidence(), "status": "invalid_response"}


async def _call_llm_with_tools(
    system_prompt: str,
    user_msg: str,
    model: str = "flash",
) -> _ToolCallResult | None:
    """调用 ReAct agent（带工具），返回最终 JSON 与 IndustryKG 工具证据。

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
        messages = result.get("messages", [])
        # ReAct agent 返回 messages 列表，取最后一条 AI 消息
        text = ""
        for msg in reversed(messages) if isinstance(messages, list) else []:
            if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content.strip():
                text = msg.content
                break
        parsed = _parse_json(text)
        return _ToolCallResult(
            parsed=parsed,
            industry_graph_evidence=_extract_industry_graph_evidence(messages),
        )
    except Exception:
        logger.exception("llm_call_with_tools_failed", model=model)
        return None


async def _analyze_understanding(user_msg: str) -> dict[str, object] | None:
    """Call 1: 事件理解（flash 模型，无工具）。"""
    return await _call_llm_no_tools(EVENT_UNDERSTANDING_PROMPT, user_msg, model="flash")


async def _analyze_transmission(
    user_msg: str,
    understanding: dict[str, object],
    *,
    external_evidence: list[dict[str, object]] | None = None,
) -> dict[str, object] | None:
    """Call 2: 传导路径分析（deep 模型，ReAct + 工具）。

    external_evidence: 第一阶段产物——由代码确定性调用图谱后注入的证据。
    若提供，则直接作为 industryGraphEvidence（不再依赖 ReAct 从 messages
    提取），但 ReAct 仍可自主调用工具做补充查询。
    """
    ud = json.dumps(understanding, ensure_ascii=False)
    prompt = EVENT_TRANSMISSION_PROMPT.replace("{understanding}", ud)
    result = await _call_llm_with_tools(prompt, user_msg, model="deep")
    if result:
        # 优先使用外部注入的证据（代码确定性图谱查询）；仅当
        # 外部未提供时回退到 ReAct 从 messages 提取（兜底）。
        evidence = (
            external_evidence
            if external_evidence is not None
            else (result.industry_graph_evidence or [_not_queried_industry_graph_evidence()])
        )
        if isinstance(result.parsed, dict):
            transmission = dict(result.parsed)
            transmission["industryGraphEvidence"] = evidence
            return transmission
        logger.warning("transmission_not_dict")
        return {"industryGraphEvidence": evidence, "chain": []}
    logger.warning("transmission_not_dict")
    return {"industryGraphEvidence": [_not_queried_industry_graph_evidence()], "chain": []}


async def _analyze_history(
    user_msg: str, understanding: dict[str, object]
) -> list[object] | None:
    """Call 3: 历史复盘（flash 模型，ReAct + 工具）。"""
    ud = json.dumps(understanding, ensure_ascii=False)
    prompt = EVENT_HISTORY_PROMPT.replace("{understanding}", ud)
    tool_call_result = await _call_llm_with_tools(prompt, user_msg, model="flash")
    result = tool_call_result.parsed if tool_call_result else None
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
    return ""


# ── 播报摘要校验（总函数） ──


def _truncate_at_sentence_boundary(text: str, max_len: int) -> str:
    """在句子边界截断文本。

    在 max_len 范围内查找最后一个句子结束符（。！？），
    若找到则截断至该位置；否则原样返回（由调用方判断是否有效）。
    """
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    for sep in ("。", "！", "？"):
        idx = truncated.rfind(sep)
        if idx > 0:
            return truncated[: idx + 1]
    return truncated


def _validate_podcast_brief(
    brief: str,
    understanding: dict[str, object] | None,
    conclusion: str,
) -> tuple[str, bool]:
    """总函数：确保 podcast_brief len() ∈ [150, 200]。

    确定性策略（无 LLM 重试）：
    - [150, 200] → (原样, True)
    - > 200 → 句子边界截断；若截断后<150 → (截断, False)
    - < 150 → 用 understanding.summary + conclusion 从事实扩充
      - 扩充后在 [150, 200] → (扩充, True)
      - 仍 < 150 → (扩充, False)，不持久化为 completed 演示记录
      - 扩充后 > 200 → 句子边界截断后重新判断
    - 无上下文可扩充 → (原文本, False)

    Returns:
        (validated_brief, can_persist):
        can_persist=False 表示摘要无法确定性地满足 [150,200]，
        不应作为合格 completed 演示数据持久化。
    """
    brief_len = len(brief)

    if 150 <= brief_len <= 200:
        return brief, True

    if brief_len > 200:
        truncated = _truncate_at_sentence_boundary(brief, 200)
        if len(truncated) >= 150:
            logger.warning(
                "podcast_brief_truncated",
                original_len=brief_len, final_len=len(truncated),
            )
            return truncated, True
        logger.warning(
            "podcast_brief_truncated_below_range",
            original=brief_len, truncated=len(truncated),
        )
        return truncated, False

    # brief_len < 150: 从已有事件事实扩充
    summary = str(understanding.get("summary", "")) if understanding else ""
    parts = [brief]
    if summary:
        parts.append(f"事件概要：{summary}")
    if conclusion:
        parts.append(f"投资判断：{conclusion}")

    padded = "。".join(p for p in parts if p)
    padded_len = len(padded)

    if 150 <= padded_len <= 200:
        logger.warning("podcast_brief_padded", original_len=brief_len, final_len=padded_len)
        return padded, True

    if padded_len > 200:
        truncated = _truncate_at_sentence_boundary(padded, 200)
        if len(truncated) >= 150:
            logger.warning(
                "podcast_brief_padded_truncated",
                original_len=brief_len, final_len=len(truncated),
            )
            return truncated, True
        logger.warning(
            "podcast_brief_unfixable_truncated",
            original=brief_len, truncated=len(truncated),
        )
        return truncated, False

    # padded_len < 150: 无法用已有事实补足
    logger.warning(
        "podcast_brief_unfixable",
        original_len=brief_len,
        padded_len=padded_len,
    )
    return padded, False


def _is_valid_cached_event_report(cached: dict[str, object]) -> bool:
    """判断缓存是否为有效事件报告，兼容无 ``event_generated`` 字段的旧缓存。

    修改前的真实旧缓存不包含 event_generated / event_persisted / event_cached /
    event_id 任何运行时状态字段，仅含业务结构（event_understanding、
    event_transmission、event_podcast_brief 等）。直接把缺 event_generated 判为
    生成失败会导致旧缓存无法走幂等补写。

    判定规则：
    1. 显式 ``event_generated`` 存在时以其值为准（新缓存）。
    2. 否则按真实业务结构校验：event_understanding 为非空 dict，
       且 event_podcast_brief 为非空字符串 —— 视为有效旧缓存（event_generated=True）。
    """
    if "event_generated" in cached:
        return bool(cached["event_generated"])
    # 旧缓存：按真实业务结构校验其是否为有效报告
    understanding = cached.get("event_understanding")
    if not isinstance(understanding, dict) or not understanding:
        return False
    brief = cached.get("event_podcast_brief")
    if not isinstance(brief, str) or not brief.strip():
        return False
    return True


def _normalize_cached_event_transmission(
    cached: dict[str, object],
    event_id: str,
    event_source: str,
) -> None:
    """让旧缓存的传导链也遵循当前 IndustryKG 证据边界。"""
    transmission = cached.get("event_transmission")
    if not isinstance(transmission, dict):
        return

    normalized = transform_to_frontend(
        None,
        transmission,
        None,
        None,
        {"eventId": event_id, "title": "", "source": event_source},
    ).get("event_transmission")
    if isinstance(normalized, dict):
        cached["event_transmission"] = normalized


def _has_verifiable_cached_graph_boundary(cached: dict[str, object]) -> bool:
    """缓存只有可审计的一跳图谱版本时才能复用其派生结论。"""
    transmission = cached.get("event_transmission")
    if not isinstance(transmission, dict):
        return False
    if transmission.get("industry_graph_boundary_version") != _INDUSTRY_GRAPH_BOUNDARY_VERSION:
        return False

    normalized = transform_to_frontend(
        None,
        transmission,
        None,
        None,
        {"eventId": "", "title": "", "source": ""},
    ).get("event_transmission")
    evidence = normalized.get("industryGraphEvidence") if isinstance(normalized, dict) else None
    if not isinstance(evidence, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("status") == "found"
        and item.get("degraded") is False
        and item.get("scope") == "one_hop"
        and item.get("source") == "IndustryKGService"
        and isinstance(item.get("graphVersion"), str)
        and bool(item["graphVersion"].strip())
        for item in evidence
    )


def _constrain_transmission_for_downstream(
    transmission: dict[str, object] | None,
    event_id: str,
    event_source: str,
) -> dict[str, object] | None:
    """将传导结果先收敛到图谱事实边界，再交给后续派生步骤。"""
    normalized = transform_to_frontend(
        None,
        transmission,
        None,
        None,
        {"eventId": event_id, "title": "", "source": event_source},
    ).get("event_transmission")
    return normalized if isinstance(normalized, dict) else None


# ── 主入口 ──


async def run(state: AgentState) -> dict[str, object]:
    """事件分析 Agent 主入口。

    5 个 LLM 调用 → transform_to_frontend → 返回 analysis_reports。

    所有返回路径提供显式状态：
    - event_generated: 生成了结构完整、非降级的事件报告
    - event_persisted: 落库成功
    - event_cached: 缓存命中或写入成功
    - event_id: 事件唯一标识（evt_xxxxxxxx）
    """
    messages = state.get("messages", [])
    if not messages:
        return {
            "final_response": "请提供需要分析的事件描述。",
            "analysis_reports": {
                "event_generated": False,
                "event_persisted": False,
                "event_cached": False,
                "event_id": "",
            },
        }

    # 提取用户消息文本——复用 utils/message.py，同时支持 HumanMessage 和 dict
    # （scheduler 和手动入口传 {"role":"user","content":"..."} dict）
    user_msg = extract_last_human_message(messages).strip()
    if not user_msg:
        return {
            "final_response": "请提供需要分析的事件描述。",
            "analysis_reports": {
                "event_generated": False,
                "event_persisted": False,
                "event_cached": False,
                "event_id": "",
            },
        }

    # 从初始 state 读取来源元数据（由 event_conduction 从 major_events.url 传入），
    # 用于 event_meta.source 落库真实来源 URL，而非硬编码空字符串。
    initial_reports = state.get("analysis_reports", {})
    event_source = ""
    if isinstance(initial_reports, dict):
        event_source = str(initial_reports.get("event_source", ""))

    # 预生成 event_id（即使降级也提供，便于追踪）
    event_id = f"evt_{hashlib.md5(user_msg.encode()).hexdigest()[:8]}"

    try:
        # 缓存检查
        cached = await get_cached_event(user_msg)
        if cached and _has_verifiable_cached_graph_boundary(cached):
            logger.info("event_cache_hit", event_preview=user_msg[:50])
            podcast_brief = str(cached.get("event_podcast_brief", ""))
            # 从缓存恢复 event_id（旧缓存可能没有）
            cached_event_id = str(cached.get("event_id", event_id))
            cached_persisted = bool(cached.get("event_persisted", False))
            # 兼容旧缓存：缺 event_generated 时按真实业务结构校验有效性，
            # 不能直接把缺字段判为生成失败（旧缓存根本没有该字段）
            cached_generated = _is_valid_cached_event_report(cached)
            _normalize_cached_event_transmission(
                cached,
                cached_event_id,
                event_source,
            )

            # 幂等补写：旧缓存缺少 event_persisted 或值为 False 时，重试落库
            if cached_generated and not cached_persisted:
                logger.info("event_cache_idempotent_repersist", event_id=cached_event_id)
                # 从缓存重建 event_meta 用于补写
                cached_understanding = cached.get("event_understanding")
                cached_meta: dict[str, object] = {
                    "eventId": cached_event_id,
                    "title": str(cached_understanding.get("summary", ""))[:50]
                    if isinstance(cached_understanding, dict)
                    else "",
                    "source": event_source,
                }
                cached_persisted = await persist_event_report(
                    cached_event_id, cached_meta, user_msg, cached
                )
                # 更新缓存中的 persisted 状态
                cached["event_persisted"] = cached_persisted
                await set_cached_event(user_msg, cached)

            return {
                "final_response": podcast_brief,
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    **cached,
                    "event_cached": True,
                    "event_generated": cached_generated,
                    "event_persisted": cached_persisted,
                    "event_id": cached_event_id,
                },
            }
        if cached:
            logger.info("event_cache_boundary_unverified", event_preview=user_msg[:50])

        # ── 5 个独立 LLM 调用 ──

        # Call 1: 事件理解（flash, no tools）
        # P1-1：understanding 失败重试一次，避免单次 LLM 抖动直接丢掉整个事件。
        understanding = await _analyze_understanding(user_msg)
        understanding_retry = False
        if not understanding:
            logger.warning("event_understanding_failed_retry", event_preview=user_msg[:50])
            understanding_retry = True
            understanding = await _analyze_understanding(user_msg)
        if not understanding:
            logger.warning("event_understanding_failed", event_preview=user_msg[:50])
            return {
                "final_response": "事件分析暂时不可用，请稍后重试",
                "analysis_reports": {
                    "event_generated": False,
                    "event_complete": False,
                    "can_persist": False,
                    "event_persisted": False,
                    "event_cached": False,
                    "event_id": event_id,
                    "event_error": {
                        "stage": "understanding",
                        "reason": "understanding LLM call failed after retry",
                    },
                },
            }

        # Call 2: 传导路径（deep, ReAct + tools）
        # ── Phase 1 稳定性升级：代码确定性图谱查询 ──
        # 从 understanding 提取核心行业名，强制调用后端 IndustryKG，
        # 消除 LLM ReAct 跳过 get_industry_chain 导致 not_queried 的问题。
        graph_evidence: list[dict[str, object]] | None = None
        if understanding:
            core_industry = str(understanding.get("coreIndustry", ""))
            # 兼容 understanding 输出未必包含 coreIndustry 字段的场景
            if not core_industry:
                logger.info("event_no_core_industry_in_understanding",
                            event_preview=user_msg[:50])
            else:
                evidence_item = await resolve_industry_graph_evidence(core_industry)
                graph_evidence = [evidence_item]
                logger.info("event_graph_resolver_called",
                            coreIndustry=core_industry,
                            kg_status=evidence_item.get("status"))
        # ──────────────────────────────────────────
        transmission = await _analyze_transmission(
            user_msg, understanding, external_evidence=graph_evidence,
        )
        constrained_transmission = _constrain_transmission_for_downstream(
            transmission,
            event_id,
            event_source,
        )

        # Call 3: 历史复盘（flash, ReAct + tools）
        history = await _analyze_history(user_msg, understanding)

        # Call 4: 投资建议（flash, no tools, 注入前 3 步结果）
        investment = await _analyze_investment(
            understanding,
            constrained_transmission,
            history,
        )

        # Call 5: 播报摘要（flash, no tools, 注入理解摘要 + 投资结论）
        conclusion = ""
        if investment and isinstance(investment, dict):
            conclusion = str(investment.get("conclusion", ""))
        podcast_brief = await _generate_podcast(understanding, conclusion)
        podcast_brief, can_persist = _validate_podcast_brief(
            podcast_brief, understanding, conclusion
        )

        # ── 构建前端对齐的 analysis_reports ──
        # 标题严格来自 understanding.summary（纯业务标题），
        # 缺失时显式降级为空字符串，绝不回退到 user_msg 或指令前缀。
        title = (
            str(understanding.get("summary", ""))
            if understanding and isinstance(understanding, dict)
            else ""
        )
        event_meta: dict[str, object] = {
            "eventId": f"evt_{hashlib.md5(user_msg.encode()).hexdigest()[:8]}",
            "title": title[:50] if title else "",
            "source": event_source,
            # 事件元数据扩展：source_name（来源名称）/ event_type（事件类型枚举）
            # 由 Understanding LLM 生成（见 EVENT_UNDERSTANDING_PROMPT）；
            # source_name 缺失时兜底"未知来源"，不阻断分析结果保存。
            "source_name": str(understanding.get("source_name", "")).strip() or "未知来源",
            "event_type": str(understanding.get("event_type", "")).strip(),
        }
        analysis_reports = transform_to_frontend(
            understanding,
            constrained_transmission,
            history,
            investment,
            event_meta,
        )
        analysis_reports["event_podcast_brief"] = podcast_brief
        event_transmission = analysis_reports.get("event_transmission")
        if isinstance(event_transmission, dict):
            event_transmission["industry_graph_boundary_version"] = (
                _INDUSTRY_GRAPH_BOUNDARY_VERSION
            )

        # P0-1：拆分"分析完成"与"展示完整"
        # event_generated / event_complete：事件分析流程是否完成
        #   = understanding 成功（已前置校验）+ event_id 存在 + title 存在
        # can_persist：仅表示前端展示完整性（podcast_brief ∈ [150,200] AND title 非空），
        #   不再阻断分析结果保存——即使展示字段不合规，完整 analysis_reports 仍会落库。
        if not title:
            logger.warning(
                "event_title_missing_cannot_persist",
                event_id=event_meta.get("eventId", ""),
            )
            can_persist = False

        event_id = str(event_meta.get("eventId", ""))
        event_generated = bool(event_id) and bool(title)
        event_complete = event_generated
        event_persisted = False
        event_cached = False
        event_persist_error: dict[str, object] | None = None

        if event_generated:
            # 分析完成即保存完整 analysis_reports（不因展示字段缺失而丢弃）
            analysis_reports["event_id"] = event_id
            analysis_reports["event_generated"] = True
            analysis_reports["event_complete"] = True
            analysis_reports["can_persist"] = can_persist
            analysis_reports["event_persisted"] = False
            # 缓存写入，便于下次命中时判断是否需要幂等补写
            event_cached = await set_cached_event(user_msg, analysis_reports)
            event_persisted = await persist_event_report(
                event_id, event_meta, user_msg, analysis_reports
            )
            analysis_reports["event_persisted"] = event_persisted
            if event_persisted:
                # 落库成功后更新缓存中的 persisted 状态
                if await set_cached_event(user_msg, analysis_reports):
                    event_cached = True
            else:
                # P1-2：落库失败必须显式记录，供 event_conduction 判定
                # success=False（该事件不进入 GI），保证 GI 输入=已确认落库事件。
                event_persist_error = {
                    "stage": "persist",
                    "reason": "persist_event_report returned False",
                }
                analysis_reports["event_persist_error"] = event_persist_error
        else:
            logger.warning(
                "event_not_generated",
                event_id=event_id,
                title_empty=not title,
                brief_len=len(podcast_brief),
            )

        if understanding_retry:
            analysis_reports["understanding_retry"] = True

        return {
            "final_response": podcast_brief,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                **analysis_reports,
                # event_generated/event_complete：分析流程完成（understanding + event_id + title）
                # can_persist：前端展示完整性（不阻断分析结果保存）
                "event_generated": event_generated,
                "event_complete": event_complete,
                "can_persist": can_persist,
                "event_persisted": event_persisted,
                "event_cached": event_cached,
                "event_id": event_id,
            },
        }
    except Exception:
        logger.exception("agent_run_failed", agent="event_analyst_v3")
        return {
            "final_response": "事件分析暂时不可用，请稍后重试",
            "analysis_reports": {
                "event_generated": False,
                "event_complete": False,
                "can_persist": False,
                "event_persisted": False,
                "event_cached": False,
                "event_id": event_id,
            },
        }
