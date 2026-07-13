"""输出解析器 — LLM 输出解析工具集

- ``parse_event_output``：事件 Agent 双层输出解析（display_report + podcast_brief）
- ``extract_major_events``：晨报 Agent 重大事件提取（从标记块或 JSON 数组）
"""

import json
import re
from collections.abc import Sequence

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
        chain = _as_list(transmission.get("chain", []))
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
            "chain": [
                {
                    "industry": str(c.get("industry", "")),
                    "relation": str(c.get("relation", "核心行业")),
                    "level": int(c.get("level", 1)),
                    "direction": _normalize_direction(
                        str(c.get("direction", "")), "chain.direction"
                    ),
                    "impactStrength": float(c.get("impactStrength", 0)),
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
