"""统一事件抓取中台 — 条件边路由入口。

按 scrape_mode 路由到不同采集方案：
    - full_daily      ：按日全量（晨报/大盘溯源/收盘汇总）
    - intraday        ：盘中增量（徐思云盘中每小时任务）
    - event_triggered ：事件触发（stock_trace 价格异动证据采集）

路由条件由调度器/调用方注入（确定性代码路由，非 LLM 决策）。
采集层确定性调用，仅重大度筛选依赖 LLM 评分（本文件只做 impact_score 过滤）。

注意：采集函数与 save_event_scrape 一律经模块引用调用（``event_scrape_sources.`` /
``event_store.``），不用 from-import 绑定 —— 测试按模块属性 patch，
from-import 会让 patch 失效（from-import 绑定陷阱，对齐 Task 4 备注2 先例）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from aistock_agent.services import event_scrape_sources, event_store
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

VALID_MODES = frozenset({"full_daily", "intraday", "event_triggered"})


def _today() -> str:
    """上海时区自然日（作为报告交易日，对齐 utils/date.py 惯例）。"""
    return shanghai_today().isoformat()


async def scrape_full_daily(score_date: str) -> dict[str, Any]:
    """按日全量采集：电报 + 东财公告/新闻 + 同花顺原创 + Tavily + 外盘。"""
    tasks = [
        event_scrape_sources.collect_cls_telegraph(score_date),
        event_scrape_sources.collect_eastmoney_judgements(score_date),
        event_scrape_sources.collect_ths_original(score_date),
        event_scrape_sources.collect_tavily(score_date),
    ]
    if score_date == _today():
        tasks.append(event_scrape_sources.collect_global_markets())

    results = await asyncio.gather(*tasks, return_exceptions=True)
    events: list[event_store.EventRecord] = []
    for res in results:
        if isinstance(res, BaseException):
            logger.warning("scrape_source_failed", error=str(res))
            continue
        events.extend(res)

    major = [ev for ev in events if event_store.is_major_event(ev)]
    logger.info("event_scrape_full_daily", total=len(events), major=len(major))
    return await event_store.save_event_scrape(major, score_date)


async def scrape_intraday(score_date: str) -> dict[str, Any]:
    """盘中增量采集：仅电报 + 东财新增，去重后落库（供传导更新）。"""
    cls_events = await event_scrape_sources.collect_cls_telegraph(score_date)
    em_events = await event_scrape_sources.collect_eastmoney_judgements(score_date)
    events = [
        ev for ev in (cls_events + em_events) if event_store.is_major_event(ev)
    ]
    logger.info("event_scrape_intraday", total=len(events))
    return await event_store.save_event_scrape(events, score_date)


async def scrape_event_triggered(event: dict[str, Any]) -> dict[str, Any]:
    """事件触发采集：stock_trace 价格异动窗口的证据（新闻+公告）。

    证据全量入库（用户裁决：stock_trace 溯源需普通事件作证据，
    豁免 is_major_event 筛选，本分支不做 impact_score 过滤）。
    只保留与标的关联的事件：symbol 命中 payload.symbol 或 involved_keywords
    （与 load_event_scrape_by_symbol 双匹配语义一致）。
    """
    symbol = str(event.get("symbol", ""))
    score_date = str(event.get("score_date") or _today())
    events = await event_scrape_sources.collect_eastmoney_judgements(score_date)
    # 只保留与标的关联的事件（symbol 命中 payload.symbol / involved_keywords）
    if symbol:
        lowered = symbol.lower()
        events = [
            ev
            for ev in events
            if lowered in str(ev.get("payload", {}).get("symbol", "")).lower()
            or any(
                lowered in str(k).lower()
                for k in ev.get("involved_keywords", [])
            )
        ]
    logger.info("event_scrape_triggered", symbol=symbol, count=len(events))
    return await event_store.save_event_scrape(events, score_date)


async def run_event_scrape(
    scrape_mode: str,
    *,
    score_date: str | None = None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """条件边路由入口。

    Args:
        scrape_mode: full_daily / intraday / event_triggered。
        score_date: 交易日（YYYY-MM-DD），默认今天。
        event: event_triggered 模式的事件载荷（含 symbol 等）。

    Returns:
        {"scrape_mode", "persisted", "deduped", "error"}。
    """
    if scrape_mode not in VALID_MODES:
        raise ValueError(f"unknown scrape_mode: {scrape_mode!r}")

    day = score_date or _today()
    if scrape_mode == "full_daily":
        result = await scrape_full_daily(day)
    elif scrape_mode == "intraday":
        result = await scrape_intraday(day)
    else:
        result = await scrape_event_triggered(event or {})

    logger.info("event_scrape_done", scrape_mode=scrape_mode, **result)
    return {"scrape_mode": scrape_mode, **result}
