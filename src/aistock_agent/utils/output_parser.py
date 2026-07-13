"""Event Agent 双层输出解析器

解析 LLM 返回的 JSON 块（display_report + podcast_brief）。
"""

import json
import re

import structlog

from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


def parse_event_output(messages: list[object]) -> tuple[dict[str, object] | None, str | None]:
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
