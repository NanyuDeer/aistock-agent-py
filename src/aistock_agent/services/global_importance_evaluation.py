"""全局重要性评估服务 — 投资者视角当日最大利好/最大利空事件识别

职责：
1. 接收当天 Event Conduction Pipeline 返回的已完成事件结果（外部注入）
2. 构建 Global Importance Prompt 所需输入结构（build_global_importance_input）
3. 调用 LLM 识别两个独立方向的焦点事件（eval_global_importance_from_events）：
   - top_bullish_event：当日最大利好事件（direction=bullish）
   - top_bearish_event：当日最大利空事件（direction=bearish）
4. 持久化结果到 DB

设计原则（2026-08-05 当天事件池重构）：
    GI 只比较当天 Morning 产生并经过 Event Conduction 分析完成的事件，
    禁止使用近 7 天 event_conduction 数据。
    ``eval_global_importance_from_events(events)`` 是 pipeline 主入口，
    入参 events 必须来自 event_analysis_pipeline 的当天传导结果。
    ``persist_global_importance_evaluation(events=...)`` 向下兼容：
    当 events 为 None 时回退到 DB 查询（用于 _test_run 手动调试）。
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

# 查询近 N 天的 event_conduction 报告（历史兼容，仅用于 _test_run 手动入口）
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

    .. deprecated::
        自 2026-08-05 当天事件池重构起，此函数仅保留用于 ``_test_run`` 手动调试入口。
        Pipeline 路径已改为 ``eval_global_importance_from_events(events)``，
        直接接收当天 event_conduction pipeline 的外部注入结果，不再主动查询历史 DB。

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
    events: list[dict[str, object]] | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> dict[str, object]:
    """全局重要性评估主入口。

    支持两种模式：
    1. **当天事件池模式（推荐）**：events 非空时，仅使用该列表做 LLM 判断，
       不查询 DB（当日事件必须由 pipeline 外部注入）。
    2. **DB 查询模式（历史兼容）**：events=None 时回退到
       build_global_importance_input(lookback_days) 查近 N 天 DB 报告。

    逻辑：
    1. 获取事件集合（外部注入或 DB 查询）
    2. events 为空时直接返回，不调用 LLM
    3. 构造 Prompt → 调用 quick_think
    4. 解析 LLM JSON 结果 → 识别最大利好/最大利空两个事件

    Args:
        events: 当天 event_conduction pipeline 结果列表（由 eval_global_importance_from_events 传入）。
                为 None 时回退 DB 查询。
        lookback_days: DB 回看天数（仅在 events=None 时生效）。

    Returns:
        {
            "as_of": "...",
            "summary": "...",
            "top_bullish_event": {...} | None,   # 当日最大利好事件
            "top_bearish_event": {...} | None,   # 当日最大利空事件
        }
    """
    # ── 步骤 1: 获取事件集合 ──
    as_of = date.today().isoformat()
    if events is not None:
        global_input = {"as_of": as_of, "events": events}
    else:
        global_input = await build_global_importance_input(lookback_days)

    event_list = global_input.get("events", [])
    if not isinstance(event_list, list) or not event_list:
        logger.warning("global_importance_skip_empty", days=lookback_days if events is None else 0)
        return {"top_bullish_event": None, "top_bearish_event": None}

    logger.info(
        "global_importance_start",
        event_count=len(event_list),
        source="external_inject" if events is not None else "db_query",
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
            "top_bullish_event": None,
            "top_bearish_event": None,
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
                "top_bullish_event": None,
                "top_bearish_event": None,
                "error": "LLM 返回非 dict 结构",
            }

        summary = str(parsed.get("summary", ""))

        # ── 提取新 Schema 字段：top_bullish_event（最大利好） ──
        raw_bullish = parsed.get("top_bullish_event")
        top_bullish_event: dict[str, object] | None = None
        if isinstance(raw_bullish, dict) and raw_bullish.get("event_id"):
            top_bullish_event = {
                "event_id": str(raw_bullish.get("event_id", "")),
                "direction": str(raw_bullish.get("direction", "")),
                "importance_level": str(raw_bullish.get("importance_level", "")),
                "reason": str(raw_bullish.get("reason", "")),
            }

        # ── 提取新 Schema 字段：top_bearish_event（最大利空） ──
        raw_bearish = parsed.get("top_bearish_event")
        top_bearish_event: dict[str, object] | None = None
        if isinstance(raw_bearish, dict) and raw_bearish.get("event_id"):
            top_bearish_event = {
                "event_id": str(raw_bearish.get("event_id", "")),
                "direction": str(raw_bearish.get("direction", "")),
                "importance_level": str(raw_bearish.get("importance_level", "")),
                "reason": str(raw_bearish.get("reason", "")),
            }

        logger.info(
            "global_importance_done",
            has_bullish=top_bullish_event is not None,
            has_bearish=top_bearish_event is not None,
        )

        return {
            "as_of": as_of,
            "summary": summary,
            "top_bullish_event": top_bullish_event,
            "top_bearish_event": top_bearish_event,
        }
    except Exception as e:
        logger.error(
            "global_importance_parse_failed",
            error=str(e),
            text_preview=text[:500],
            exc_info=True,
        )
        return {
            "top_bullish_event": None,
            "top_bearish_event": None,
            "error": f"解析失败: {str(e)}",
        }


async def eval_global_importance_from_events(
    events: list[dict[str, object]],
) -> dict[str, object]:
    """从当天事件传导结果评估 Global Importance（当天事件池入口）。

    这是 pipeline 主入口，与 run_global_importance_evaluation 不同：
    本函数接收外部注入的当天 event_conduction 结果，
    对其进行排序过滤（仅保留当日事件），然后调用 LLM 评估。

    **当天校验**：每个输入事件必须满足 event_age_days == 0（即 event_date == today）。
    若 event_age_days != 0，直接过滤，防止未来出现历史事件污染。

    Args:
        events: 当天 event_conduction pipeline 的结果列表。
            每个 dict 必须包含 event_id 等 GI 输入所需字段。

    Returns:
        {top_bullish_event, top_bearish_event, as_of, summary}
    """
    # ── 当天校验：仅保留 event_age_days == 0 的事件（event_date == today）──
    today_events = [
        e for e in events
        if isinstance(e, dict) and e.get("event_age_days") == 0
    ]
    filtered_count = len(events) - len(today_events)
    if filtered_count > 0:
        logger.warning(
            "global_importance_filtered_non_today",
            total=len(events),
            today_count=len(today_events),
            filtered=filtered_count,
        )

    return await run_global_importance_evaluation(events=today_events)


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
        "summary": str(result.get("summary", "")),
        "top_bullish_event": result.get("top_bullish_event"),
        "top_bearish_event": result.get("top_bearish_event"),
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
        )
        return True
    except Exception:
        logger.warning("global_importance_persist_exception", date=report_date, exc_info=True)
        return False


async def persist_global_importance_evaluation(
    events: list[dict[str, object]] | None = None,
    lookback_days: int | None = None,
) -> dict[str, object]:
    """组合入口：执行评估 → 判断结果 → 持久化。

    # Global Importance only evaluates today's completed event conduction results.
    当 ``events`` 非空时使用外部注入的当天事件；为 None 时回退 DB 查询（_test_run 兼容）。

    流程：
    1. 调用 run_global_importance_evaluation(events=events, lookback_days=lookback_days)
    2. 若 events 为空或错误 → 不写数据库
    3. 若正常 → 调用 save_global_importance_report()

    Args:
        events: 当天 event_conduction pipeline 结果列表（推荐），为 None 且 lookback_days 为 None 时跳过评估。
        lookback_days: DB 回看天数（仅在 events=None 时生效）。默认 None，不查历史数据。

    Returns:
        同 run_global_importance_evaluation() 的结构，增加 persisted 状态。
    """
    result = await run_global_importance_evaluation(
        events=events,
        lookback_days=lookback_days if lookback_days is not None else _DEFAULT_LOOKBACK_DAYS,
    )

    has_bullish = result.get("top_bullish_event") is not None
    has_bearish = result.get("top_bearish_event") is not None
    error = result.get("error", "")

    if error:
        logger.warning("global_importance_persist_skip_error", error=error)
        return {**result, "persisted": False}

    if not has_bullish and not has_bearish:
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

    # ── 最大利好事件 ──
    bullish = result.get("top_bullish_event")
    print("\n📈 最大利好事件 (top_bullish_event):")
    if bullish:
        print(f"  event_id: {bullish.get('event_id')}")
        print(f"  direction: {bullish.get('direction')}")
        print(f"  level: {bullish.get('importance_level')}")
        print(f"  reason: {bullish.get('reason')}")
    else:
        print("  (null)")

    # ── 最大利空事件 ──
    bearish = result.get("top_bearish_event")
    print("\n📉 最大利空事件 (top_bearish_event):")
    if bearish:
        print(f"  event_id: {bearish.get('event_id')}")
        print(f"  direction: {bearish.get('direction')}")
        print(f"  level: {bearish.get('importance_level')}")
        print(f"  reason: {bearish.get('reason')}")
    else:
        print("  (null)")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(_test_run())
