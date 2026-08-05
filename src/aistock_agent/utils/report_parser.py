"""双层报告解析工具 — 兼容 schema_version 1.0 和 2.0

schema_version 1.0: content = {"text": "..."}
schema_version 2.0: content = {"display_report": {...}, "podcast_brief": "...", "schema_version": "2.0"}
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def parse_report_content(content: dict) -> tuple[str, str]:
    """解析报告 content，返回 (display_text, podcast_brief)

    兼容 1.0 单层和 2.0 双层结构。

    Args:
        content: 数据库 content 字段（JSONB 解析后的 dict）

    Returns:
        (display_text, podcast_brief) 元组
        - display_text: 前端展示用的完整文本
        - podcast_brief: 播报用摘要文本（1.0 版本可能为空字符串）
    """
    if not isinstance(content, dict):
        return ("", "")

    schema_version = content.get("schema_version", "1.0")

    if schema_version == "2.0":
        # 双层结构
        display_report = content.get("display_report", {})
        if isinstance(display_report, dict):
            summary = display_report.get("summary", "")
            details = display_report.get("details", "")
            if summary and details:
                display_text = f"{summary}\n\n{details}"
            else:
                display_text = details or summary or ""
        elif isinstance(display_report, str):
            display_text = display_report
        else:
            display_text = ""

        podcast_brief = content.get("podcast_brief", "") or ""
        return (display_text, podcast_brief)

    # 1.0 单层结构
    text = content.get("text", "") or ""
    return (text, "")


def extract_podcast_brief(content: dict) -> str:
    """只提取 podcast_brief（供 broadcast_agent 使用）

    1.0 版本返回空字符串（无播报摘要）。
    """
    _, podcast_brief = parse_report_content(content)
    return podcast_brief


def extract_display_report(content: dict) -> str:
    """只提取 display_report 文本（供 broadcast_agent 消费）

    1.0 版本返回 text 字段。
    """
    display_text, _ = parse_report_content(content)
    return display_text


def parse_dual_layer_response(final_response: str) -> dict:
    """解析 LLM 返回的双层 JSON 响应，持久化到 DB content 字段

    如果 LLM 未返回有效 JSON，降级为单层结构（display_report.details = 原文本）。

    Args:
        final_response: LLM 返回的原始文本

    Returns:
        双层 content dict，包含 display_report、podcast_brief、schema_version
    """
    # 尝试提取 JSON 块（LLM 可能包裹在 ```json ... ``` 中）
    text = final_response.strip()
    if text.startswith("```"):
        # 去掉 markdown 代码块标记
        lines = text.split("\n")
        # 去掉首行 ```json 和末尾 ```
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "display_report" in parsed:
            parsed["schema_version"] = "2.0"
            # 确保 podcast_brief 字段存在
            if "podcast_brief" not in parsed:
                parsed["podcast_brief"] = ""
            return parsed
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug("parse_dual_layer_response: JSON parse failed, fallback to text", error=str(e))

    # 降级：将纯文本作为 display_report.details
    return {
        "display_report": {
            "summary": "",
            "details": final_response,
        },
        "podcast_brief": "",
        "schema_version": "2.0",
    }


def is_dual_layer_valid(content: dict) -> bool:
    """检查双层结构是否有效（summary 非空）。

    当 LLM 返回非 JSON 纯文本时，parse_dual_layer_response 降级生成
    summary="" 的结构，此时返回 False，提示调用方进行 LLM 修复。
    """
    display = content.get("display_report")
    if not isinstance(display, dict):
        return False
    summary = display.get("summary", "")
    return isinstance(summary, str) and bool(summary.strip())


_REPAIR_PROMPT = """将以下分析文本转换为标准 JSON 格式。严格按此结构返回，不要包含 markdown 标记或额外说明：

{{
  "display_report": {{
    "summary": "20字以内的结论一句话",
    "details": "完整分析内容原样保留",
    "stocks": ["股票代码1"],
    "risks": ["风险提示1"]
  }},
  "podcast_brief": "150-200字的播报摘要"
}}

待转换文本：
{text}"""


async def repair_dual_layer_with_llm(final_response: str) -> dict | None:
    """当 parse_dual_layer_response 解析失败时，调用 quick_think LLM 将纯文本转为标准双层 JSON。

    Returns:
        修复后的双层 dict（summary 非空），或 None（修复失败）。
    """
    from langchain_core.messages import HumanMessage

    from aistock_agent.services.llm import get_quick_think

    try:
        llm = get_quick_think()
        result = await llm.ainvoke(
            [HumanMessage(content=_REPAIR_PROMPT.format(text=final_response))]
        )
        repaired_text = result.content if hasattr(result, "content") else str(result)
        repaired = parse_dual_layer_response(repaired_text)
        if is_dual_layer_valid(repaired):
            return repaired
        logger.warning("repair_dual_layer_with_llm: repaired result still invalid")
    except Exception as e:
        logger.warning("repair_dual_layer_with_llm failed", error=str(e))
    return None
