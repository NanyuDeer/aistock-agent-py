"""晨报结构化提取服务 — 从 morning 报告提取 MorningForecast。

从 node_api 读取当日 morning 报告，复用 extract_major_events 提取事件列表，
再用 quick_think LLM 推断板块方向判断和事件方向，输出 MorningForecast。

设计要点：
- 失败不阻断：任何异常返回 None，由调用方写入 missing_fields
- 缓存：提取结果缓存 Redis 2h（key=morning:forecast:YYYY-MM-DD）
- LLM 用 quick_think（gpt-4o-mini）省 token
"""

from __future__ import annotations

import json
import re
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.schemas.market_trace import MorningForecast
from aistock_agent.services.cache import (
    get_cached_morning_forecast,
    set_cached_morning_forecast,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_quick_think
from aistock_agent.utils.output_parser import extract_major_events

logger = structlog.get_logger()

# LLM 提取 prompt
_MORNING_FORECAST_EXTRACTION_PROMPT = """你是金融晨报分析助手。从晨报全文中提取结构化预测信息。

输入：
- 晨报日期：{report_date}
- 晨报摘要：{summary}
- 晨报全文：{details}
- 晨报已知事件（JSON）：{events_json}
- 晨报风险列表：{risks_json}
- 源报告 ID：{source_report_id}

请输出严格的 JSON，schema 如下：
{{
  "report_date": "YYYY-MM-DD",
  "summary": "晨报核心结论一句话",
  "major_events": [
    {{"title": "事件标题", "direction": "bullish|bearish|neutral", "affected_sectors": ["板块1", "板块2"]}}
  ],
  "sectors": [
    {{"sector": "板块名", "direction": "bullish|bearish|neutral", "note": "判断依据摘要"}}
  ],
  "risks": ["风险1", "风险2"],
  "source_report_id": "源报告 ID 或 null"
}}

规则：
1. major_events 优先复用已知事件列表，推断每个事件的 direction
2. sectors 从晨报全文推断板块方向判断（晨报原文可能没有显式板块字段）
3. 若晨报未提及任何板块方向，sectors 输出空数组
4. 只输出 JSON，禁止 markdown 代码围栏
"""


# 晨报展示侧反哺代表 target：晨报 focus 是当日市场方向，绑定市场指数画像即可复用验证闭环。
# 仅展示侧（Spec A §4.2）：不改判定、不落库、不进验证画像/迭代闭环，异常降级保持原文。
_MARKET_PROFILE_SYMBOL = "上证指数"


async def _enrich_morning_summary_with_profile(forecast: MorningForecast) -> MorningForecast:
    """Spec B §4.3 morning_forecast 展示侧反哺——市场指数画像 sufficient_sample 时在 summary 追加历史命中率。

    - 不改判定/不产指令：只把历史命中率作为展示文本追加到 summary。
    - 不进闭环：结果不写缓存、不改 MorningForecast 其它字段（缓存仍存原始 LLM 提取结果，防命中率随缓存陈旧）。
    - 红线：target 解析/读取失败 → 返回原 forecast（不阻断晨报产出）。
    """
    from aistock_agent.services.target_profile import make_target
    from aistock_agent.skills.prediction_validation import read_validation_profile

    target = make_target(_MARKET_PROFILE_SYMBOL)
    if target is None:
        return forecast
    try:
        profile = await read_validation_profile(target)
    except Exception:  # noqa: BLE001
        logger.debug("morning_forecast.enrich_profile_failed", symbol=_MARKET_PROFILE_SYMBOL,
                     exc_info=True)
        return forecast
    n = int(cast(float, profile.get("n", 0) or 0))
    if not profile.get("sufficient_sample") or n <= 0:
        return forecast
    rate = float(cast(float, profile.get("hit_rate", 0.0) or 0.0))
    extra = f"；历史大盘预判命中率 {rate:.0%}（n={n}）"
    base_summary = forecast.summary
    return forecast.model_copy(update={"summary": base_summary + extra})


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """剥离 LLM 可能包裹的 ```json ... ```。"""
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1) if match else text.strip()


async def extract_morning_forecast(report_date: str) -> MorningForecast | None:
    """从当日 morning 报告提取结构化预测。

    Args:
        report_date: 报告日期 YYYY-MM-DD

    Returns:
        MorningForecast 或 None（报告缺失/提取失败）
    """
    # 1. 检查缓存
    try:
        cached = await get_cached_morning_forecast(report_date)
        if cached is not None:
            return MorningForecast.model_validate(cached)
    except Exception as e:
        logger.debug("get_cached_morning_forecast_failed", error_class=type(e).__name__)

    # 2. 读取 morning 报告
    try:
        report = await node_api.get_analysis_report("morning", report_date)
    except Exception as e:
        logger.warning("morning_report_fetch_failed", error_class=type(e).__name__)
        return None

    if not isinstance(report, dict):
        return None

    content = report.get("content")
    if not isinstance(content, dict):
        return None

    display = content.get("display_report")
    if not isinstance(display, dict):
        return None

    summary = str(display.get("summary", ""))
    details = str(display.get("details", ""))
    risks_raw = display.get("risks")
    risks = risks_raw if isinstance(risks_raw, list) else []
    source_report_id = report.get("id")
    if not isinstance(source_report_id, str):
        source_report_id = None

    # 3. 复用 extract_major_events 提取事件
    try:
        major_events_raw = extract_major_events(details)
    except Exception as e:
        logger.warning("extract_major_events_failed", error_class=type(e).__name__)
        major_events_raw = []

    # 4. LLM 提取结构化预测
    prompt = _MORNING_FORECAST_EXTRACTION_PROMPT.format(
        report_date=report_date,
        summary=summary,
        details=details[:3000],  # 截断防止 token 爆炸
        events_json=json.dumps(major_events_raw, ensure_ascii=False),
        risks_json=json.dumps(risks, ensure_ascii=False),
        source_report_id=source_report_id,
    )

    try:
        llm = get_quick_think()
        messages = [
            SystemMessage(content="你是金融晨报分析助手，只输出 JSON。"),
            HumanMessage(content=prompt),
        ]
        ai_message = await llm.ainvoke(messages)
        # langchain BaseMessage.content 类型为 str | list[ContentBlock]，
        # _strip_code_fences 只接受 str，这里统一收敛。
        raw = ai_message.content if hasattr(ai_message, "content") else str(ai_message)
        raw_text = raw if isinstance(raw, str) else str(raw)
        cleaned = _strip_code_fences(raw_text)
        forecast = MorningForecast.model_validate_json(cleaned)
    except Exception as e:
        logger.warning("morning_forecast_llm_failed", error_class=type(e).__name__)
        return None

    # 5. 写缓存（存原始 LLM 提取结果，防止命中率随缓存陈旧）
    try:
        await set_cached_morning_forecast(report_date, forecast.model_dump(mode="json"))
    except Exception as e:
        logger.debug("set_cached_morning_forecast_failed", error_class=type(e).__name__)

    # 5b. 展示侧反哺：仅对返回结果追加历史命中率到 summary，不进闭环、不吃缓存；异常降级保持原文。
    return await _enrich_morning_summary_with_profile(forecast)
