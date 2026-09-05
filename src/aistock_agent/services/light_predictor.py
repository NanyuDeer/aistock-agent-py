"""自选股洞察轻量预判任务（阶段 2，2026-09-03）。

对两次打点（11:30/15:05）后"当日异动/涨停 ∪ 重大利好/利空资讯"的自选股（Node
``/internal/stock-trace/light-predict-targets`` 按 symbol 去重）批量生成条件化轻量预判：
- 有异动/涨停事件（含交集）→ 归因驱动（primary_cause + 当日重大资讯补充 + 盘口），回写
  ``stock_trace_events.forecast``（slot 级 upsert，midday/close 互不覆盖）
- 仅重大资讯（无事件）→ 事件影响驱动，回写当日该股最新一条重大资讯
  ``stock_info_judgements.forecast``（slot 级 upsert）

LLM 走 quick_think 单次 + json_mode（LightForecast schema），失败/校验不过跳过该股不阻断。
调度：11:40（scheduler_light_predict_midday_cron）+ 15:20（scheduler_light_predict_close_cron）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.light_predict import PREDICTION_LIGHT_PROMPT
from aistock_agent.schemas.prediction import LightForecast
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_quick_think, with_chat_structured_output
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger(__name__)

_SLOTS = ("midday", "close")

# ── 盘口/特征确定性组装（零 LLM） ──


def _num(value: Any) -> float | None:
    """任意值 → float；不可解析/NaN → None。"""
    try:
        number = float(value)
        return number if number == number else None  # NaN → None
    except (TypeError, ValueError):
        return None


def _quote_feature(quote: dict[str, Any] | None) -> dict[str, Any]:
    """腾讯中文键行情 → 精简特征（缺字段容错）。"""
    if not quote:
        return {}
    return {
        "latest_price": _num(quote.get("最新价")),
        "change_pct": _num(quote.get("涨跌幅")),
    }


def _kline_feature(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """日 K（升序）→ 均线/区间/量能派生。无数据返回 {}（prompt 缺量能时不产 volume 条件）。"""
    if not rows:
        return {}
    closes = [c for c in (_num(r.get("close")) for r in rows) if c is not None]
    if not closes:
        return {}
    feature: dict[str, Any] = {}
    if len(closes) >= 5:
        feature["ma5"] = round(sum(closes[-5:]) / 5, 2)
    if len(closes) >= 10:
        feature["ma10"] = round(sum(closes[-10:]) / 10, 2)
    if len(closes) >= 20:
        feature["ma20"] = round(sum(closes[-20:]) / 20, 2)
    window_tail = rows[-20:]
    high_vals = [_num(r.get("high")) for r in window_tail]
    low_vals = [_num(r.get("low")) for r in window_tail]
    high_ok = [h for h in high_vals if h is not None]
    low_ok = [low for low in low_vals if low is not None]
    if high_ok:
        feature["high_20d"] = max(high_ok)
    if low_ok:
        feature["low_20d"] = min(low_ok)
    vols = [v for v in (_num(r.get("vol")) for r in rows) if v is not None]
    if len(vols) >= 10:
        recent = sum(vols[-5:]) / 5
        prev = sum(vols[-10:-5]) / 5
        if prev > 0:
            feature["vol_recent5_avg"] = round(recent, 2)
            feature["vol_prev5_avg"] = round(prev, 2)
    return feature


async def assemble_features(target: dict[str, Any]) -> dict[str, Any]:
    """单只股票预判输入组装：事件归因 + 当日资讯 + 盘口（quote + kline，flow 首版不接入）。"""
    symbol = str(target.get("symbol", ""))
    raw_event = target.get("event")
    event = raw_event if isinstance(raw_event, dict) else None
    raw_intel = target.get("intel") or []
    intel = [item for item in raw_intel if isinstance(item, dict)]
    features: dict[str, Any] = {
        "symbol": symbol,
        "stock_name": str(target.get("stock_name") or ""),
        "scenario_type": "trace" if event else "intel",
    }
    if event:
        features["event"] = {
            "direction": event.get("direction"),
            "change_pct": event.get("change_pct"),
            "severity": event.get("severity"),
            "is_limit_up": event.get("is_limit_up"),
            "analysis_status": event.get("analysis_status"),
            "primary_cause": event.get("primary_cause"),
        }
    if intel:
        features["intel"] = [
            {
                "title": str(item.get("title") or ""),
                "summary": str(item.get("summary") or ""),
                "impact": str(item.get("impact") or ""),
            }
            for item in intel
        ]
    quote_feature = _quote_feature(await node_api.get_quote(symbol))
    if quote_feature:
        features["quote"] = quote_feature
    kline_feature = _kline_feature(await node_api.get_stock_kline(symbol, days=30))
    if kline_feature:
        features["kline"] = kline_feature
    return features


# ── LLM 生成与校验 ──


def build_forecast_payload(
    slot: str, scenario_type: str, forecast: LightForecast,
) -> dict[str, Any]:
    """LightForecast → Node forecast slot 落库负载（slot 键互不覆盖由 Node upsert 保证）。"""
    return {
        "schema_version": "1",
        "scenario": scenario_type,
        "summary": forecast.summary,
        "conditions": [c.model_dump(mode="json") for c in forecast.conditions],
        "generated_at": datetime.now(UTC).isoformat(),
        "slot": slot,
    }


async def _generate_forecast(
    target: dict[str, Any],
) -> tuple[LightForecast, str] | None:
    """单只 LLM 生成；返回 (forecast, scenario_type)；任何失败（LLM/解析/超时）→ None。"""
    features = await assemble_features(target)
    scenario_type = str(features.get("scenario_type") or "intel")
    llm = get_quick_think()
    messages = [
        SystemMessage(content=PREDICTION_LIGHT_PROMPT),
        HumanMessage(content=json.dumps(features, ensure_ascii=False, indent=2)),
    ]
    structured = with_chat_structured_output(llm, LightForecast)
    try:
        forecast = cast(LightForecast, await structured.ainvoke(messages))
    except Exception as exc:  # noqa: BLE001 —— 单只失败仅告警跳过（对齐 LLM 失败跳过硬约束）
        logger.warning(
            "light_predict_llm_failed", symbol=features.get("symbol"), error=str(exc),
        )
        return None
    return forecast, scenario_type


async def _process_target(target: dict[str, Any], slot: str) -> bool:
    """单只预判 + 回写。返回是否成功落库。"""
    generated = await _generate_forecast(target)
    if generated is None:
        return False
    forecast_obj, scenario_type = generated
    symbol = str(target.get("symbol", ""))
    raw_event = target.get("event")
    event = raw_event if isinstance(raw_event, dict) else None
    payload = build_forecast_payload(slot, scenario_type, forecast_obj)
    if event:
        ok = await node_api.set_event_forecast(
            str(event.get("eventId") or ""), slot, payload,
        )
        logger.info("light_predict_written_event", symbol=symbol, slot=slot, ok=bool(ok))
        return bool(ok)
    # 仅资讯股：写当日该股最新一条重大资讯行（Node targets 的 intel 按 published_at DESC）
    raw_intel = target.get("intel") or []
    intel = [item for item in raw_intel if isinstance(item, dict)]
    if not intel:
        logger.warning("light_predict_no_landing", symbol=symbol, slot=slot)
        return False
    first = intel[0]
    ok = await node_api.set_judgement_forecast(
        int(first.get("id") or 0), slot, payload,
    )
    logger.info(
        "light_predict_written_intel",
        symbol=symbol, slot=slot, judgement_id=first.get("id"), ok=bool(ok),
    )
    return bool(ok)


async def run_light_prediction(slot: str) -> int:
    """两次打点后批量轻量预判入口（slot ∈ midday|close）。返回成功落库数。"""
    if slot not in _SLOTS:
        raise ValueError(f"invalid light predict slot: {slot}")
    trade_date = str(shanghai_today())
    targets = await node_api.list_light_predict_targets(trade_date)
    if not targets:
        logger.info("light_predict_no_targets", slot=slot, trade_date=trade_date)
        return 0
    written = 0
    for target in targets:
        try:
            if await _process_target(target, slot):
                written += 1
        except Exception as exc:  # noqa: BLE001 —— 单只失败不阻断整批
            logger.warning(
                "light_predict_target_failed",
                symbol=target.get("symbol"), slot=slot, error=str(exc),
            )
    logger.info(
        "light_predict_done",
        slot=slot, trade_date=trade_date, targets=len(targets), written=written,
    )
    return written
