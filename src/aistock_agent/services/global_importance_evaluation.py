"""全局重要性评估服务 — 投资者视角焦点事件识别

职责：
1. 查询近 7 天 event_conduction 报告（_load_recent_event_reports）
2. 提取关键字段，构建 Global Importance Prompt 所需输入结构（build_global_importance_input）
3. 调用 LLM 识别两个独立的焦点事件（run_global_importance_evaluation）：
   - current_focus_event：当前最值得关注的事件（新事件优先）
   - ongoing_significant_event：仍在影响市场的持续事件
4. 持久化结果到 DB
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.global_importance import GLOBAL_IMPORTANCE_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_quick_think
from aistock_agent.utils.output_parser import _parse_json

logger = structlog.get_logger()

# 查询近 N 天的 event_conduction 报告
_DEFAULT_LOOKBACK_DAYS = 7


def _safe_str(value: object, default: str = "") -> str:
    """安全提取字符串，空值返回默认值。"""
    if isinstance(value, str):
        return value
    return default


def _safe_float(value: object, default: float = 0.0) -> float:
    """安全提取浮点数。"""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return default


def _safe_list(value: object) -> list[Any]:
    """安全提取列表。"""
    return value if isinstance(value, list) else []


def _safe_dict(value: object) -> dict[str, Any]:
    """安全提取字典。"""
    return value if isinstance(value, dict) else {}


def _calc_event_age_days(publish_time_str: str) -> int:
    """计算事件距今的天数。

    根据 publishTime 计算到今天的差距（天）。
    无法解析时默认返回 7（超出默认查询范围的旧事件）。
    """
    if not publish_time_str:
        return _DEFAULT_LOOKBACK_DAYS
    try:
        # 兼容 ISO 格式（2026-07-23T08:56:04.512755）和日期格式（2026-07-23）
        if "T" in publish_time_str:
            pub_dt = datetime.fromisoformat(publish_time_str.split(".")[0])
        else:
            pub_dt = datetime.strptime(publish_time_str, "%Y-%m-%d")
        delta = date.today() - pub_dt.date()
        return max(0, delta.days)
    except (ValueError, TypeError):
        return _DEFAULT_LOOKBACK_DAYS


def _extract_event_input(content: dict[str, object]) -> dict[str, object] | None:
    """从单条 event_conduction 的 content JSONB 提取 Global Importance 输入。

    字段映射（严谨对照 DB 存储路径）：
    - event_id              → content.eventId
    - summary               → content.analysis_reports.event_understanding.summary
    - original_event        → content.event（原始用户消息文本）
    - impact_industries     → content.analysis_reports.event_transmission.chain[].industry（去重）
    - impact_chain          → content.analysis_reports.event_transmission.chain[]（industry/direction/impactStrength）
    - key_variables         → content.analysis_reports.event_transmission.variables[]（name/direction/strength）
    - mechanism             → content.analysis_reports.event_transmission.mechanism
    - investment_rating     → content.analysis_reports.event_investment.rating
    - investment_conclusion → content.analysis_reports.event_investment.conclusion

    Args:
        content: agent_analysis_reports 表中 content 列的完整 JSONB 值。

    Returns:
        可供 Global Importance Prompt 输入的 event dict，或 None（关键字段缺失）。
    """
    event_id = _safe_str(content.get("eventId"))
    if not event_id:
        logger.warning("global_importance_skip_no_event_id")
        return None

    # ── 安全提取各模块 ──
    ar = _safe_dict(content.get("analysis_reports"))
    understanding = _safe_dict(ar.get("event_understanding"))
    transmission = _safe_dict(ar.get("event_transmission"))
    investment = _safe_dict(ar.get("event_investment"))

    # ── event_understanding 字段 ──
    summary = _safe_str(understanding.get("summary"))

    # ── original_event: content.event 是用户原始输入消息 ──
    original_event = _safe_str(content.get("event"))

    # ── event_transmission 字段 ──
    mechanism = _safe_str(transmission.get("mechanism"))

    # 影响行业列表（从 chain[] 去重提取 industry 名）
    raw_chain = _safe_list(transmission.get("chain"))
    impact_industries = list({
        _safe_str(item.get("industry"))
        for item in raw_chain
        if isinstance(item, dict) and item.get("industry")
    })

    # 产业链影响链（保留 industry/direction/impactStrength）
    impact_chain: list[dict[str, object]] = []
    for item in raw_chain:
        if not isinstance(item, dict):
            continue
        industry = _safe_str(item.get("industry"))
        if not industry:
            continue
        impact_chain.append({
            "industry": industry,
            "direction": _safe_str(item.get("direction")),
            "impact_strength": _safe_float(item.get("impactStrength")),
        })

    # 关键变量
    raw_variables = _safe_list(transmission.get("variables"))
    key_variables: list[dict[str, object]] = []
    for v in raw_variables:
        if not isinstance(v, dict):
            continue
        key_variables.append({
            "name": _safe_str(v.get("name")),
            "direction": _safe_str(v.get("direction")),
            "strength": _safe_float(v.get("strength")),
        })

    # ── event_investment 字段 ──
    investment_rating = _safe_str(investment.get("rating"))
    investment_conclusion = _safe_str(investment.get("conclusion"))

    # 关键字段缺失判定：至少有 summary 或 original_event 之一
    if not summary and not original_event:
        logger.warning(
            "global_importance_skip_no_content",
            event_id=event_id,
        )
        return None

    # ── 事件时间字段 ──
    publish_time_str = _safe_str(content.get("publishTime"))
    event_age_days = _calc_event_age_days(publish_time_str)

    return {
        "event_id": event_id,
        "event_time": publish_time_str,
        "event_age_days": event_age_days,
        "summary": summary,
        "original_event": original_event,
        "impact_industries": impact_industries,
        "impact_chain": impact_chain,
        "key_variables": key_variables,
        "mechanism": mechanism,
        "investment_rating": investment_rating,
        "investment_conclusion": investment_conclusion,
    }


async def _load_recent_event_reports(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> list[dict[str, object]]:
    """查询近 N 天的 event_conduction 报告。

    使用 node_api.list_analysis_reports 逐天查询，合并去重。
    只保留 status='completed' 且 event_generated 为 True 的有效报告。

    Args:
        lookback_days: 回看天数，默认 7 天。

    Returns:
        结构化的 content 列表（agent_analysis_reports 表中 content 列的值）。
    """
    today = date.today()
    seen_event_ids: set[str] = set()
    all_contents: list[dict[str, object]] = []

    for day_offset in range(lookback_days):
        query_date = today - timedelta(days=day_offset)
        date_str = query_date.isoformat()

        try:
            reports = await node_api.list_analysis_reports("event_conduction", date_str)
        except Exception:
            logger.warning(
                "global_importance_list_failed",
                report_date=date_str,
                exc_info=True,
            )
            continue

        for report in reports:
            if not isinstance(report, dict):
                continue
            content = report.get("content")
            if not isinstance(content, dict):
                continue
            event_id = _safe_str(content.get("eventId"))
            if not event_id or event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            all_contents.append(content)

    if all_contents:
        logger.info(
            "global_importance_loaded",
            total=len(all_contents),
            days_range=lookback_days,
        )
    else:
        logger.warning("global_importance_no_events", days_range=lookback_days)

    return all_contents


async def build_global_importance_input(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> dict[str, object]:
    """构建 Global Importance Prompt 所需输入结构。

    这是 Adapter 层核心函数，职责：
    1. 从 DB 查询近 N 天 event_conduction 报告
    2. 提取并映射字段到 Prompt 输入格式
    3. 返回可直接序列化为 JSON 的 dict

    Args:
        lookback_days: 回看天数，默认 7 天。

    Returns:
        {
            "as_of": "2026-07-23",
            "events": [
                {
                    "event_id": "evt_xxxxxxxx",
                    "event_time": "2026-07-23T08:56:04",
                    "event_age_days": 0,
                    "summary": "...",
                    "original_event": "...",
                    "impact_industries": [...],
                    "impact_chain": [...],
                    "key_variables": [...],
                    "mechanism": "...",
                    "investment_rating": "...",
                    "investment_conclusion": "...",
                },
            ]
        }
    """
    contents = await _load_recent_event_reports(lookback_days)

    events: list[dict[str, object]] = []
    for content in contents:
        event_input = _extract_event_input(content)
        if event_input is not None:
            events.append(event_input)

    return {
        "as_of": date.today().isoformat(),
        "events": events,
    }


async def run_global_importance_evaluation(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> dict[str, object]:
    """全局重要性评估主入口。

    逻辑：
    1. 调用 build_global_importance_input() 获取近 N 天事件集合
    2. events 为空时直接返回，不调用 LLM
    3. 构造 Prompt → 调用 quick_think
    4. 解析 LLM JSON 结果 → 识别两个焦点事件 + 构建向后兼容 events

    Args:
        lookback_days: 回看天数，默认 7 天。

    Returns:
        {
            "as_of": "...",
            "total_events": 2,
            "summary": "...",
            "current_focus_event": {...} | None,       # 新 Schema
            "ongoing_significant_event": {...} | None,  # 新 Schema
            "events": [{...}, ...],                     # 向后兼容
        }
    """
    # ── 步骤 1: 获取近 N 天事件集合 ──
    global_input = await build_global_importance_input(lookback_days)
    events = global_input.get("events", [])
    if not isinstance(events, list) or not events:
        logger.warning("global_importance_skip_empty", days=lookback_days)
        return {"events": [], "current_focus_event": None, "ongoing_significant_event": None}

    logger.info(
        "global_importance_start",
        event_count=len(events),
        days=lookback_days,
    )

    # ── 步骤 2: 构造 Prompt ──
    input_json = json.dumps(global_input, ensure_ascii=False, indent=2)
    user_message = f"请识别当前最值得关注的焦点事件：\n\n{input_json}"

    # ── 步骤 3: 调用 LLM ──
    try:
        llm = get_quick_think()
        result = await llm.ainvoke([
            SystemMessage(content=GLOBAL_IMPORTANCE_PROMPT),
            HumanMessage(content=user_message),
        ])
        text = str(result.content) if hasattr(result, "content") else str(result)
    except Exception as e:
        logger.error("global_importance_llm_failed", error=str(e), exc_info=True)
        return {
            "events": [],
            "current_focus_event": None,
            "ongoing_significant_event": None,
            "error": f"LLM 调用失败: {str(e)}",
        }

    # ── 步骤 4: 解析 JSON ──
    try:
        parsed = _parse_json(text)
        if not isinstance(parsed, dict):
            logger.warning(
                "global_importance_parse_not_dict",
                text_preview=text[:300],
            )
            return {
                "events": [],
                "current_focus_event": None,
                "ongoing_significant_event": None,
                "error": "LLM 返回非 dict 结构",
            }

        summary = str(parsed.get("summary", ""))

        # ── 提取新 Schema 字段：current_focus_event ──
        raw_focus = parsed.get("current_focus_event")
        current_focus_event: dict[str, object] | None = None
        if isinstance(raw_focus, dict) and raw_focus.get("event_id"):
            current_focus_event = {
                "event_id": str(raw_focus.get("event_id", "")),
                "importance_level": str(raw_focus.get("importance_level", "")),
                "impact_scope": str(raw_focus.get("impact_scope", "")),
                "direction": str(raw_focus.get("direction", "")),
                "reason": str(raw_focus.get("reason", "")),
                "selection_reason": str(raw_focus.get("selection_reason", "")),
            }

        # ── 提取新 Schema 字段：ongoing_significant_event ──
        raw_ongoing = parsed.get("ongoing_significant_event")
        ongoing_significant_event: dict[str, object] | None = None
        if isinstance(raw_ongoing, dict) and raw_ongoing.get("event_id"):
            ongoing_significant_event = {
                "event_id": str(raw_ongoing.get("event_id", "")),
                "importance_level": str(raw_ongoing.get("importance_level", "")),
                "impact_scope": str(raw_ongoing.get("impact_scope", "")),
                "direction": str(raw_ongoing.get("direction", "")),
                "reason": str(raw_ongoing.get("reason", "")),
                "selection_reason": str(raw_ongoing.get("selection_reason", "")),
            }

        # ── 构建向后兼容的 events 数组 ──
        backward_events: list[dict[str, object]] = []
        for idx, item in enumerate(
            filter(None, [current_focus_event, ongoing_significant_event]),
            start=1,
        ):
            backward_events.append({
                "event_id": str(item["event_id"]),
                "rank": idx,
                "importance_score": _score_from_level(
                    str(item.get("importance_level", "")),
                ),
                "importance_level": str(item.get("importance_level", "")),
                "impact_scope": str(item.get("impact_scope", "")),
                "impact_period": "",
                "direction": str(item.get("direction", "")),
                "reason": str(item.get("reason", "")),
            })

        total = len(backward_events)
        logger.info(
            "global_importance_done",
            total_events=total,
            has_focus=current_focus_event is not None,
            has_ongoing=ongoing_significant_event is not None,
        )

        return {
            "as_of": date.today().isoformat(),
            "total_events": total,
            "summary": summary,
            "current_focus_event": current_focus_event,
            "ongoing_significant_event": ongoing_significant_event,
            "events": backward_events,
        }
    except Exception as e:
        logger.error(
            "global_importance_parse_failed",
            error=str(e),
            text_preview=text[:500],
            exc_info=True,
        )
        return {
            "events": [],
            "current_focus_event": None,
            "ongoing_significant_event": None,
            "error": f"解析失败: {str(e)}",
        }


def _score_from_level(level: str) -> float:
    """将 importance_level 转换为 importance_score（向后兼容用）。"""
    mapping = {"critical": 9.0, "important": 6.0, "notable": 3.0}
    return mapping.get(level, 0.0)


# ── 持久化 ──


async def save_global_importance_report(
    result: dict[str, object],
    report_date: str | None = None,
) -> bool:
    """将 Global Importance 评估结果持久化到 agent_analysis_reports。

    复用 node_api.save_analysis_report()，report_type='global_importance'。
    user_id = None（公共报告，同晨报模式），upsert 按 report_type + report_date + '' 去重。

    Args:
        result: run_global_importance_evaluation() 的返回结果。
        report_date: 报告日期（YYYY-MM-DD），默认当天。

    Returns:
        True 表示持久化成功，False 表示失败。
    """
    from datetime import datetime

    if report_date is None:
        report_date = datetime.now().strftime("%Y-%m-%d")

    content: dict[str, object] = {
        "as_of": str(result.get("as_of", report_date)),
        "total_events": result.get("total_events", 0),
        "summary": str(result.get("summary", "")),
        "current_focus_event": result.get("current_focus_event"),
        "ongoing_significant_event": result.get("ongoing_significant_event"),
        "events": result.get("events", []),
    }

    try:
        saved = await node_api.save_analysis_report(
            report_type="global_importance",
            report_date=report_date,
            content=content,
            user_id=None,
            data_source="global_importance_agent",
            status="completed",
        )
        if saved is None:
            logger.warning("global_importance_persist_failed_none", date=report_date)
            return False
        logger.info(
            "global_importance_persisted",
            date=report_date,
            total_events=content["total_events"],
        )
        return True
    except Exception:
        logger.warning("global_importance_persist_exception", date=report_date, exc_info=True)
        return False


async def persist_global_importance_evaluation(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> dict[str, object]:
    """组合入口：执行评估 → 判断结果 → 持久化。

    流程：
    1. 调用 run_global_importance_evaluation()
    2. 若 events 为空或错误 → 不写数据库
    3. 若正常 → 调用 save_global_importance_report()

    Args:
        lookback_days: 回看天数，默认 7 天。

    Returns:
        同 run_global_importance_evaluation() 的结构，增加 persisted 状态。
    """
    result = await run_global_importance_evaluation(lookback_days)

    has_focus = result.get("current_focus_event") is not None
    has_ongoing = result.get("ongoing_significant_event") is not None
    error = result.get("error", "")

    if error:
        logger.warning("global_importance_persist_skip_error", error=error)
        return {**result, "persisted": False}

    if not has_focus and not has_ongoing:
        logger.warning("global_importance_persist_skip_empty")
        return {**result, "persisted": False}

    persisted = await save_global_importance_report(result)
    return {**result, "persisted": persisted}


# ── 临时测试入口 ──


async def _test_run() -> None:
    """运行一次全局重要性评估并打印结果。

    用法：python -m aistock_agent.services.global_importance_evaluation
    """
    print("=" * 60)
    print("Global Importance Evaluation — 测试运行")
    print("=" * 60)

    result = await run_global_importance_evaluation(lookback_days=7)

    error = result.get("error", "")

    if error:
        print(f"\n❌ 错误: {error}")
        return

    print(f"\n📝 摘要: {result.get('summary', '')}")

    # ── 当前焦点事件 ──
    focus = result.get("current_focus_event")
    print("\n🎯 当前焦点事件 (current_focus_event):")
    if focus:
        print(f"  event_id: {focus.get('event_id')}")
        print(f"  level: {focus.get('importance_level')}")
        print(f"  scope: {focus.get('impact_scope')}")
        print(f"  direction: {focus.get('direction')}")
        print(f"  reason: {focus.get('reason')}")
        print(f"  selection_reason: {focus.get('selection_reason')}")
    else:
        print("  (null)")

    # ── 重大持续事件 ──
    ongoing = result.get("ongoing_significant_event")
    print("\n🔄 重大持续事件 (ongoing_significant_event):")
    if ongoing:
        print(f"  event_id: {ongoing.get('event_id')}")
        print(f"  level: {ongoing.get('importance_level')}")
        print(f"  scope: {ongoing.get('impact_scope')}")
        print(f"  direction: {ongoing.get('direction')}")
        print(f"  reason: {ongoing.get('reason')}")
        print(f"  selection_reason: {ongoing.get('selection_reason')}")
    else:
        print("  (null)")

    print("\n📋 向后兼容 events 数组:")
    for ev in result.get("events", []):
        print(
            f"  #{ev.get('rank')} | score={ev.get('importance_score'):.1f} | "
            f"{ev.get('impact_scope')} | "
            f"{ev.get('direction')} | {ev.get('event_id')}"
        )
        if ev.get("reason"):
            print(f"     → {ev.get('reason')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(_test_run())
