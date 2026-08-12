"""事件抓取数据源封装 — 统一事件抓取中台的采集层。

原则：复用现有 /internal/* 内部接口与已有 service，不重写爬虫。
采集层为确定性调用（非 LLM tool），由 event_scraper 条件边按 scrape_mode 编排。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_store import EventRecord, normalize_event
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()


def _extract_items(resp: object, key: str = "items") -> list[Any]:
    """从 node_api 解包响应中安全提取列表字段（非 dict / 非 list → []）。

    node_api.get 返回 ``dict[str, object] | None``，逐层 isinstance 收窄，
    避免 mypy strict 的 object-not-iterable 与 None.get 报错。
    """
    if not isinstance(resp, dict):
        return []
    value = resp.get(key)
    if isinstance(value, list):
        return value
    return []


async def collect_cls_telegraph(score_date: str) -> list[EventRecord]:
    """财联社当日全量电报（按日全量）。失败降级 /internal/news/latest。

    Args:
        score_date: 交易日（YYYY-MM-DD）。

    Returns:
        归一化 EventRecord 列表（source=cls）。
    """
    try:
        resp = await node_api.get(
            f"/internal/news/telegraph?date={score_date}&limit=200"
        )
        items = _extract_items(resp)
        degraded = bool(resp.get("degraded")) if isinstance(resp, dict) else True
        if degraded or not items:
            raise RuntimeError(f"telegraph degraded or empty: {resp}")
    except Exception:  # noqa: BLE001
        logger.warning("cls_telegraph_failed_fallback_latest", date=score_date)
        resp = await node_api.get("/internal/news/latest")
        items = _extract_items(resp)

    events: list[EventRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw: dict[str, Any] = dict(item)
        raw.setdefault("title", raw.get("content", ""))
        raw.setdefault("url", "")
        event = normalize_event(raw, source="cls", score_date=score_date)
        if event is not None:
            events.append(event)
    return events


async def collect_eastmoney_judgements(score_date: str) -> list[EventRecord]:
    """东方财富公告/新闻（复用已 AI 研判结果，不重复分析）。

    读取 stock_info_judgements 表（个股情报管线已闭环）。
    Node 端 StockMonitorService.getEvents 返回 ``{"total": N, "events": [...]}``
    （键名 events，非 items）；alerts 接口不支持按 published_at 窗口查询，
    取最近 20 条跨全部日期，Python 侧按行 published_at/event_time 过滤，
    仅保留与 score_date 同日的行（避免昨日/前日陈旧行被标记为当日事件反复入库）。
    """
    try:
        resp = await node_api.get("/internal/monitor/alerts?days=1")
        rows = _extract_items(resp, key="events")
    except Exception as exc:  # noqa: BLE001
        logger.exception("eastmoney_judgements_failed", error=str(exc))
        return []

    events: list[EventRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw: dict[str, Any] = dict(row)
        # 当日窗口过滤（C1 修复后真实键名 events；I1 防跨日陈旧行误标当日）：
        # 时间格式可能是 "2026-08-12 10:00:00" 或 "2026-08-12T10:00:00"，
        # 统一按日期前缀 startswith(score_date) 判断；无时间字段的行保守保留
        published = str(
            raw.get("event_time") or raw.get("published_at") or ""
        ).strip()
        if published and not published.startswith(score_date):
            continue
        # 对齐字段：Node mapJudgementToEvent 输出 detail_url（非 url/link），
        # normalize_event 只认 url/link → 这里补齐，否则东财事件 url 恒空
        # （I2：大盘溯源 causal_ready_count 不计入、stock_trace canonicalUrl 缺失）
        raw.setdefault("url", raw.get("detail_url") or "")
        # 对齐字段：ai_impact → direction 映射
        impact = str(raw.get("ai_impact", "")).strip()
        if "利好" in impact and "重大" in impact:
            raw["direction"] = "positive"
            raw["impact_score"] = 5
        elif "利空" in impact and "重大" in impact:
            raw["direction"] = "negative"
            raw["impact_score"] = 5
        elif "利好" in impact:
            raw["direction"] = "positive"
            raw["impact_score"] = 3
        elif "利空" in impact:
            raw["direction"] = "negative"
            raw["impact_score"] = 3
        else:
            raw["direction"] = "neutral"
            raw["impact_score"] = 1
        event = normalize_event(raw, source="eastmoney", score_date=score_date)
        if event is not None:
            events.append(event)
    return events


async def collect_ths_original(score_date: str) -> list[EventRecord]:
    """同花顺原创/涨停雷达（博主源，insight 模块已爬取）。

    读取 Node 新增接口 GET /internal/insight/sources?date=YYYY-MM-DD
    （查 watchlist_insight_sources 表按 trade_date 过滤，见 Step 3b Node 配合）。
    """
    try:
        resp = await node_api.get(f"/internal/insight/sources?date={score_date}")
        rows = _extract_items(resp)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ths_original_failed", error=str(exc))
        return []

    events: list[EventRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw: dict[str, Any] = dict(row)
        raw.setdefault("direction", "neutral")
        # Min-1：显式映射 summary/involved_keywords（Node 源字段为 content/keywords）。
        # keywords 是 JSONB，可能已解析为 list 或仍为 JSON 字符串，做防御解析。
        raw["summary"] = str(raw.get("content") or raw.get("summary") or "").strip()
        keywords = raw.get("keywords")
        if isinstance(keywords, list):
            raw["involved_keywords"] = [str(k) for k in keywords if isinstance(k, str)]
        elif isinstance(keywords, str):
            try:
                parsed = json.loads(keywords)
            except (TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                raw["involved_keywords"] = [
                    str(k) for k in parsed if isinstance(k, str)
                ]
            else:
                raw["involved_keywords"] = []
        else:
            raw["involved_keywords"] = []
        event = normalize_event(raw, source="ths_original", score_date=score_date)
        if event is not None:
            events.append(event)
    return events


async def collect_tavily(score_date: str) -> list[EventRecord]:
    """Tavily 全网检索（复用大盘溯源查询词模板，参数化日期）。

    TavilyService.search 为同步阻塞调用，用 asyncio.to_thread 包装防阻塞事件循环
    （对齐 douyin_video 的「阻塞 IO 必须 to_thread」工程约束）。
    """
    from aistock_agent.services.tavily import TavilyService

    queries = [
        f"{score_date} 中国 资本市场 政策 产业 公告",
        f"{score_date} 全球股市 利率 汇率 大宗商品 地缘风险",
    ]
    events: list[EventRecord] = []
    for query in queries:
        try:
            result = await asyncio.to_thread(
                TavilyService().search, query, topic="news", max_results=5
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("tavily_search_failed", query=query, error=str(exc))
            continue
        hits = _extract_items(result, key="results")
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            raw = {
                "title": str(hit.get("title", "")),
                "summary": str(hit.get("content", "")),
                "url": str(hit.get("url", "")),
            }
            event = normalize_event(raw, source="tavily", score_date=score_date)
            if event is not None:
                events.append(event)
    return events


async def collect_global_markets() -> list[EventRecord]:
    """外盘指数（隔夜美股/恒生/亚太），仅作为盘前档的行情事实事件。

    复用 market_tools.collect_global_market_facts（get_global_markets Tool 的
    结构化事实来源：[{ticker, name, price, change_pct, observed_at}]），
    不调 @tool 包装的字符串输出，保证 direction/impact_score 可计算。

    分级入库（用户裁决）：波动 >= 1% 记为重大事实（impact_score=5）过
    is_major_event 筛选落库；< 1% 记普通事实（impact_score=1），
    在 full_daily 分支被 is_major_event 过滤不落库。
    """
    from aistock_agent.tools.market_tools import collect_global_market_facts

    score_date = shanghai_today().isoformat()
    events: list[EventRecord] = []
    try:
        facts = await collect_global_market_facts(datetime.now(UTC))
        for fact in facts:
            name = str(fact.get("name") or fact.get("ticker") or "")
            # Min-5：无名称（name/ticker 均为空）的行情事实无标题可归一化，跳过
            if not name:
                continue
            price = fact.get("price")
            change_pct = fact.get("change_pct")
            pct = 0.0
            if isinstance(change_pct, int | float | str):
                try:
                    pct = float(change_pct)
                except (TypeError, ValueError):
                    pct = 0.0
            raw = {
                "title": f"{name} 隔夜表现",
                "summary": f"{name}: {price} ({pct}%)",
                "url": "",
                "direction": (
                    "positive" if pct > 0 else "negative" if pct < 0 else "neutral"
                ),
                # Important 2：波动 >= 1% 记为重大事实（impact_score=5）过
                # is_major_event 落库；< 1% 为 1（full_daily 分支被过滤不落库）
                "impact_score": 5 if abs(pct) >= 1 else 1,
            }
            event = normalize_event(raw, source="global_markets", score_date=score_date)
            if event is not None:
                events.append(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("global_markets_failed", error=str(exc))
    return events
