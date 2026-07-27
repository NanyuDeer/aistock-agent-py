"""输出解析器 — LLM 输出解析工具集

- ``parse_event_output``：事件 Agent 双层输出解析（display_report + podcast_brief）
- ``extract_major_events``：晨报 Agent 重大事件提取（从标记块或 JSON 数组）
"""

import json
import re
from collections.abc import Sequence
from typing import cast

import structlog
from langchain_core.messages import BaseMessage

from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


def parse_event_output(
    messages: Sequence[BaseMessage | dict[str, str]],
) -> tuple[dict[str, object] | None, str | None]:
    """从 LLM 消息列表解析 display_report + podcast_brief。

    解析策略（逐级回退）：
    1. 提取最后一条 AI 消息，尝试整段 JSON 解析
    2. 如果失败，正则匹配 JSON 块（花括号平衡）
    3. 再失败则返回 (None, None)

    Returns:
        (display_report, podcast_brief) 元组，解析失败均返回 None。
    """
    text = extract_final_ai_response(messages)
    if not text:
        logger.warning("event_output_parse_empty_text")
        return (None, None)

    # 策略 1: 整段 JSON 解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return _extract_fields(parsed)
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 正则匹配 JSON 块（花括号平衡）
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return _extract_fields(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    logger.warning("event_output_parse_failed", text_preview=text[:200])
    return (None, None)


def _extract_fields(parsed: dict[str, object]) -> tuple[dict[str, object] | None, str | None]:
    """从解析后的 dict 提取 display_report 和 podcast_brief"""
    display = parsed.get("display_report")
    brief = parsed.get("podcast_brief")

    display_dict = display if isinstance(display, dict) else None
    brief_str = brief if isinstance(brief, str) else (str(brief) if brief else None)

    return (display_dict, brief_str)


def extract_major_events(text: str) -> list[dict[str, object]]:
    """从晨报文本中提取重大事件列表。

    解析策略（逐级回退）：
    1. 查找 ``<!--MAJOR_EVENTS_START-->...<!--MAJOR_EVENTS_END-->`` 标记块，JSON 解析
    2. 兼容：正则匹配 JSON 数组 ``[{...}]``
    3. 都失败返回空列表

    从 ``agents/workers/morning.py`` 迁出，供 morning run() 和 snapshot_builder 复用。
    """
    # 策略 1: 标记块
    match = re.search(
        r'<!--MAJOR_EVENTS_START-->\s*\n?(.*?)\n?\s*<!--MAJOR_EVENTS_END-->',
        text, re.DOTALL,
    )
    if match:
        try:
            events = json.loads(match.group(1))
            if isinstance(events, list):
                return [e for e in events if isinstance(e, dict)]
        except (json.JSONDecodeError, TypeError):
            pass

    # 策略 2: 兼容 JSON 数组
    json_match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
    if json_match:
        try:
            events = json.loads(json_match.group(0))
            if isinstance(events, list):
                return [e for e in events if isinstance(e, dict)]
        except (json.JSONDecodeError, TypeError):
            pass

    return []


# ── 通用 JSON 解析（供 event.py 各 helper 复用） ──


def _parse_json(text: str) -> dict[str, object] | list[object] | None:
    """从 LLM 输出文本中提取 JSON 对象或数组。

    解析策略（与 parse_event_output 一致）：
    1. 去掉 markdown 代码块（```json ... ``` 或 ``` ... ```）
    2. 整段 JSON 解析
    3. 正则匹配 JSON 块（花括号/方括号平衡）
    4. 都失败返回 None
    """
    if not text:
        return None

    # 去掉 markdown 代码块
    cleaned = re.sub(r'```(?:json)?\s*\n?', '', text)
    cleaned = re.sub(r'\n?\s*```', '', cleaned)
    cleaned = cleaned.strip()

    # 策略 1: 整段解析
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict | list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 正则匹配 JSON 块
    for pattern in [r'\{.*\}', r'\[.*\]']:
        match = re.search(pattern, cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict | list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

    logger.warning("json_parse_failed", text_preview=text[:200])
    return None


# ── 方向映射 ──

_DIRECTION_MAP: dict[str, str] = {
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "neutral",
    "利好": "bullish",
    "利空": "bearish",
    "中性": "neutral",
    "positive": "positive",
    "negative": "negative",
}


def _normalize_direction(value: str, field: str) -> str:
    """方向值标准化，未知值打 log 后降级为 neutral"""
    normalized = _DIRECTION_MAP.get(value)
    if normalized is None:
        logger.warning("direction_normalize_fallback", field=field, raw=value)
        return "neutral"
    return normalized


def _as_list(value: object) -> list[object]:
    """从 dict.get() 结果中安全提取 list，非 list 值返回空列表。

    供 transform_to_frontend 内部列表推导使用，避免 mypy strict 下
    dict[str, object].get() 返回 object 不可迭代的问题。
    """
    return value if isinstance(value, list) else []


_INDUSTRY_GRAPH_MISSING_BOUNDARY = "本次未取得 IndustryKG 图谱事实，上下游关系未展开，不能补造。"
_INDUSTRY_GRAPH_DIRECT_RELATION_BOUNDARY = (
    "仅一跳直接关系，方向和强度是分析推断，不构成确定因果。"
)
_INDUSTRY_GRAPH_DEGRADED_STATUSES = {
    "not_queried",
    "invalid_input",
    "not_found",
    "authentication_failed",
    "upstream_failed",
    "timeout",
    "request_failed",
    "invalid_response",
}


def _degraded_industry_graph_evidence(
    status: str,
    missing_boundary: object = None,
) -> dict[str, object]:
    """构造未取得 IndustryKG 事实时的统一证据边界。"""
    if isinstance(missing_boundary, str) and missing_boundary.strip():
        boundary = missing_boundary
    else:
        boundary = _INDUSTRY_GRAPH_MISSING_BOUNDARY
    return {
        "status": status,
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": boundary,
    }


def _normalize_industry_graph_evidence(value: object) -> list[dict[str, object]]:
    """规范化仅由 Transmission 工具消息注入的 IndustryKG 证据。"""
    raw_evidence = value if isinstance(value, list) else []
    evidence: list[dict[str, object]] = []

    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status == "found":
            source = item.get("source")
            industry = item.get("industry")
            upstream = item.get("upstream")
            downstream = item.get("downstream")
            if _is_valid_found_industry_graph_evidence(
                item, industry, upstream, downstream
            ):
                evidence.append(
                    {
                        "status": "found",
                        "degraded": False,
                        "scope": "one_hop",
                        "source": source,
                        "industry": industry,
                        "upstream": upstream,
                        "downstream": downstream,
                        "graphVersion": item.get("graphVersion"),
                        "updatedAt": item.get("updatedAt"),
                        "missingBoundary": None,
                    }
                )
                continue
            evidence.append(_degraded_industry_graph_evidence("invalid_response"))
            continue

        normalized_status = (
            status if isinstance(status, str) and status in _INDUSTRY_GRAPH_DEGRADED_STATUSES
            else "invalid_response"
        )
        evidence.append(
            _degraded_industry_graph_evidence(
                normalized_status,
                item.get("missingBoundary"),
            )
        )

    return evidence or [_degraded_industry_graph_evidence("not_queried")]


def _is_valid_found_industry_graph_evidence(
    evidence: dict[object, object],
    industry: object,
    upstream: object,
    downstream: object,
) -> bool:
    """校验可用于约束链路的 IndustryKG 一跳 found 证据。"""
    if (
        evidence.get("scope") != "one_hop"
        or evidence.get("degraded") is not False
        or evidence.get("source") != "IndustryKGService"
        or not isinstance(upstream, list)
        or not isinstance(downstream, list)
    ):
        return False
    return (
        _is_valid_industry_node(industry)
        and all(_is_valid_industry_node(node, requires_leading_stocks=True) for node in upstream)
        and all(
            _is_valid_industry_node(node, requires_leading_stocks=True)
            for node in downstream
        )
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


def _industry_names(nodes: object) -> set[str]:
    """从一侧 IndustryKG 节点提取可验证的行业名称。"""
    if not isinstance(nodes, list):
        return set()
    return {
        name
        for node in nodes
        if isinstance(node, dict)
        for name in [node.get("name")]
        if isinstance(name, str) and name
    }


def _as_string_keyed_dict(value: object) -> dict[str, object] | None:
    """将 JSON 对象收窄为字符串键的字典。"""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return value


def _constrain_chain_by_industry_graph(
    chain: list[object],
    evidence: list[dict[str, object]],
) -> list[dict[str, object]]:
    """用一跳图谱事实约束模型输出的行业链节。"""
    found_evidence = [item for item in evidence if item["status"] == "found"]
    if not found_evidence:
        core_chain: list[dict[str, object]] = []
        for raw_item in chain:
            item = _as_string_keyed_dict(raw_item)
            if item is not None and item.get("relation") == "核心行业":
                core_chain.append({**item, "relation": "核心行业", "level": 1})
        return core_chain

    # 按中心行业 ID 建立一跳邻接表；名称仅用于将 LLM 展示名称解析为唯一 ID。
    # 不再把多个中心的上下游合并成全局集合，否则 B 的邻接行业会被误判成 A 的直接关系，
    # 造成多中心归属串链。
    center_adjacency: dict[str, dict[str, set[str]]] = {}
    center_ids_by_name: dict[str, set[str]] = {}
    for item in found_evidence:
        industry = item["industry"]
        if not isinstance(industry, dict):
            continue
        center_id = industry.get("id")
        center_name = industry.get("name")
        if not (
            isinstance(center_id, str)
            and center_id
            and isinstance(center_name, str)
            and center_name
        ):
            continue
        center_adjacency[center_id] = {
            "upstream": _industry_names(item["upstream"]),
            "downstream": _industry_names(item["downstream"]),
        }
        center_ids_by_name.setdefault(center_name, set()).add(center_id)

    constrained: list[dict[str, object]] = []
    # 当前归属的中心行业 ID。每遇到核心行业先无条件清空，避免未验证核心
    # 让后续邻接沿用上一中心；同名中心映射多个 ID 时也必须 fail-closed。
    current_center_id: str | None = None
    for raw_item in chain:
        item = _as_string_keyed_dict(raw_item)
        if item is None:
            continue
        if item.get("relation") == "核心行业":
            current_center_id = None
            industry_name = item.get("industry")
            center_ids = (
                center_ids_by_name.get(industry_name)
                if isinstance(industry_name, str)
                else None
            )
            if center_ids is not None and len(center_ids) == 1:
                current_center_id = next(iter(center_ids))
                constrained.append({**item, "relation": "核心行业", "level": 1})
            continue

        industry = item.get("industry")
        if not isinstance(industry, str) or current_center_id is None:
            # 没有前置中心行业证据支持，丢弃该邻接行业。
            continue
        adjacency = center_adjacency[current_center_id]
        if industry in adjacency["upstream"]:
            constrained.append(
                {
                    **item,
                    "relation": "图谱上游（直接关系）",
                    "level": 2,
                    "reason": _INDUSTRY_GRAPH_DIRECT_RELATION_BOUNDARY,
                }
            )
        elif industry in adjacency["downstream"]:
            constrained.append(
                {
                    **item,
                    "relation": "图谱下游（直接关系）",
                    "level": 2,
                    "reason": _INDUSTRY_GRAPH_DIRECT_RELATION_BOUNDARY,
                }
            )
        # 既不是当前中心行业的上游也不是下游：丢弃，避免把别的中心的邻接行业串到当前中心。
    return constrained


# ── 字段映射 ──


def transform_to_frontend(
    understanding: dict[str, object] | None,
    transmission: dict[str, object] | None,
    history: list[object] | None,
    investment: dict[str, object] | None,
    event_meta: dict[str, object],
) -> dict[str, object]:
    """将 4 个 LLM 模块输出 + 事件元信息映射为 analysis_reports。

    Args:
        understanding: Call 1 输出（EventUnderstanding JSON dict）
        transmission: Call 2 输出（TransmissionAnalysis JSON dict）
        history: Call 3 输出（HistoryEvent[] list）
        investment: Call 4 输出（InvestmentSummary JSON dict）
        event_meta: {"eventId": str, "title": str, "source": str}

    Returns:
        analysis_reports dict，结构：
        {
            "event_understanding": {...},
            "event_transmission": {...},
            "event_history": [...],
            "event_investment": {...},
        }
    """
    reports: dict[str, object] = {}

    # ── event_understanding ──
    if understanding and isinstance(understanding, dict):
        reports["event_understanding"] = {
            "summary": str(understanding.get("summary", "")),
            "coreChanges": [
                {
                    "variable": str(c.get("variable", "")),
                    "before": str(c.get("before", "")),
                    "after": str(c.get("after", "")),
                }
                for c in _as_list(understanding.get("coreChanges", []))
                if isinstance(c, dict)
            ],
        }
    else:
        reports["event_understanding"] = None

    # ── event_transmission ──
    if transmission and isinstance(transmission, dict):
        variables = _as_list(transmission.get("variables", []))
        industry_graph_evidence = _normalize_industry_graph_evidence(
            transmission.get("industryGraphEvidence")
        )
        chain = _constrain_chain_by_industry_graph(
            _as_list(transmission.get("chain", [])),
            industry_graph_evidence,
        )
        core_industry = transmission.get("coreIndustry", {})

        reports["event_transmission"] = {
            "eventId": event_meta.get("eventId", ""),
            "mechanism": str(transmission.get("mechanism", "")),
            "variables": [
                {
                    "name": str(v.get("name", "")),
                    "direction": _normalize_direction(
                        str(v.get("direction", "")), "variables.direction"
                    ),
                    "strength": float(v.get("strength", 0)),
                    "explanation": str(v.get("explanation", "")),
                }
                for v in variables
                if isinstance(v, dict)
            ],
            "coreIndustry": {
                "name": str(core_industry.get("name", "")),
                "impact": str(core_industry.get("impact", "")),
                "reason": str(core_industry.get("reason", "")),
            } if isinstance(core_industry, dict) else {"name": "", "impact": "", "reason": ""},
            "industryGraphEvidence": industry_graph_evidence,
            "chain": [
                {
                    "industry": str(c.get("industry", "")),
                    "relation": str(c.get("relation", "核心行业")),
                    "level": int(cast(str | float | int, c.get("level", 1))),
                    "direction": _normalize_direction(
                        str(c.get("direction", "")), "chain.direction"
                    ),
                    "impactStrength": float(
                        cast(str | float | int, c.get("impactStrength", 0))
                    ),
                    "reason": str(c.get("reason", "")),
                }
                for c in chain
                if isinstance(c, dict)
            ],
        }
    else:
        reports["event_transmission"] = None

    # ── event_history ──
    if history and isinstance(history, list):
        reports["event_history"] = [
            {
                "historyId": str(h.get("historyId", "")),
                "year": str(h.get("year", "")),
                "title": str(h.get("title", "")),
                "eventType": str(h.get("eventType", "")),
                "sentiment": _normalize_direction(str(h.get("sentiment", "")), "history.sentiment"),
                "industryChange": str(h.get("industryChange", "")),
                "changePercentage": float(h.get("changePercentage", 0)),
            }
            for h in history
            if isinstance(h, dict)
        ]
    else:
        reports["event_history"] = []

    # ── event_investment ──
    if investment and isinstance(investment, dict):
        focus_industries = _as_list(investment.get("focusIndustries", []))
        reports["event_investment"] = {
            "id": event_meta.get("eventId", ""),
            "conclusion": str(investment.get("conclusion", "")),
            "keyPoints": [
                str(kp) for kp in _as_list(investment.get("keyPoints", []))
            ],
            "focusIndustries": [
                {
                    "name": str(fi.get("name", "")),
                    "direction": _normalize_direction(
                        str(fi.get("direction", "")), "focusIndustries.direction"
                    ),
                    "reason": str(fi.get("reason", "")),
                }
                for fi in focus_industries
                if isinstance(fi, dict)
            ],
            "opportunities": [
                str(o) for o in _as_list(investment.get("opportunities", []))
            ],
            "risks": [
                str(r) for r in _as_list(investment.get("risks", []))
            ],
            "rating": _normalize_direction(str(investment.get("rating", "neutral")), "rating"),
        }
    else:
        reports["event_investment"] = None

    return reports
