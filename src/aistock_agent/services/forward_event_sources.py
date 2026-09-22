"""前瞻事件抓取源（spec §5.1 方案 A / §5.7）：L3 搜索兜底（自中台迁入）+ 韭研源（N1）。

迁移纪律（硬约束 9）：collect_l3_forward 对外行为与迁出前一致（query 计数软上限、
当日去重 key 日期化、空结果负缓存、解析不出日期不入库）；仅 query 族按 §5.7 扩为 6 条
（覆盖 7 类事件族）、软上限 8→12（迁出后独享配额）。
"""
from __future__ import annotations

import asyncio
import re
from datetime import date

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.services.search_cache import SearchCache

logger = structlog.get_logger()

# §5.7 query 族重定（自然语言不日期化，覆盖 7 类）
L3_FORWARD_QUERIES: tuple[str, ...] = (
    "下周 财经日历 重要事件 A股",
    "下周 A股 财报 业绩预告",
    "美联储 下周 议息 讲话 经济数据",
    "下周 宏观数据 发布 CPI PPI PMI 社融",
    "下周 政治局会议 中央经济工作会议 两会 政策",
    "下周 A股 限售解禁 新股申购 期权到期",
)
L3_QUERY_HARD_LIMIT = 6          # §5.7：与 query 族 1:1，防失控
L3_DAILY_SOFT_LIMIT = 12         # §5.7：迁出后独享配额（原 8 次/日与中台共用 → 12）

_l3_daily_count: dict[str, int] = {}


async def _run_search(query: str) -> dict[str, object]:
    """统一搜索链（§4.3）：TavilyService.search 已封装统一链；阻塞 IO 用 to_thread。"""
    from aistock_agent.services.tavily import TavilyService

    return await asyncio.to_thread(TavilyService().search, query, topic="news", max_results=5)


_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})"),
    re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
)


def _parse_event_date(text: str, ref_year: int) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        year = int(g[0]) if len(g) == 3 and len(g[0]) == 4 else ref_year
        month = int(g[0]) if len(g) == 2 else int(g[1])
        day = int(g[1]) if len(g) == 2 else int(g[2])
        try:
            # date 构造做日历合法性校验：13 月/2 月 30 日等非法日期
            # 自然抛 ValueError 落入下方 except（f-string 对已 int 值永不抛，是死代码）
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def _parse_forward_events(
    query: str, result: dict[str, object], ref_date: str
) -> list[dict[str, object]]:
    """预告解析：命中内容提日期+主题 → 事件实体（解析不出日期不入库，warning）。"""
    hits = result.get("results")
    if not isinstance(hits, list):
        return []
    ref_year = int(ref_date[:4])
    events: list[dict[str, object]] = []
    for hit in hits:
        title = str(hit.get("title") or "")
        content = str(hit.get("content") or "")
        event_date = _parse_event_date(f"{title} {content}", ref_year)
        if not event_date:
            logger.warning("forward_event_l3.no_date", query=query, title=title[:50])
            continue
        events.append({
            "event_date": event_date,
            "title": title[:80] or f"前瞻事件（{event_date}）",
            "importance": "medium",  # 抓取源封顶 medium（R3）
            "market": "CN",
            "source": "L3",
            "detail": content[:200],
        })
    return events


async def collect_l3_forward(score_date: str, cache: SearchCache) -> list[dict[str, object]]:
    """L3 前瞻捕捉：6 条前瞻 query（硬上限）→ 统一搜索链 → 解析预告 → upsert。

    - 当日去重（缓存 key 日期化）；空结果负缓存；
    - 软上限 12 次/日：超限跳过并标"L3 降级"。
    """
    today_count = _l3_daily_count.get(score_date, 0)
    if today_count >= L3_DAILY_SOFT_LIMIT:
        logger.warning("forward_event_l3.soft_limit_skip", date=score_date, count=today_count)
        return []
    parsed_events: list[dict[str, object]] = []
    for query in L3_FORWARD_QUERIES[: L3_QUERY_HARD_LIMIT]:
        if _l3_daily_count.get(score_date, 0) >= L3_DAILY_SOFT_LIMIT:
            break
        key = SearchCache.normalize_key(score_date, query)
        state = cache.get(key)
        if state is not None:  # 当日已查（ok=成功去重 / empty=负缓存）
            continue
        try:
            result = await _run_search(query)
        except Exception:
            logger.warning("forward_event_l3.search_failed", query=query)
            continue
        _l3_daily_count[score_date] = _l3_daily_count.get(score_date, 0) + 1
        outcome = str(result.get("outcome", ""))
        if outcome == "error":
            continue
        events = _parse_forward_events(query, result, score_date)
        if not events:
            cache.record(key, empty=True)  # 空结果负缓存（防同日多班重复付费）
            continue
        cache.record(key, empty=False)
        for ev in events:
            try:
                await node_api.post_calendar_event(ev)
            except Exception:
                logger.warning("forward_event_l3.post_failed", event_date=ev.get("event_date"))
        parsed_events.extend(events)
    return parsed_events


async def collect_jiuyan(score_date: str, cache: SearchCache) -> list[dict]:
    """韭研源（N1，spec §5.6）抓取入口——最小退化实现。

    控制台裁决 ②（O2 风险登记）：韭研反爬/合规评估未通过前，跳过抓取并留
    ``data_missing``，不阻断主体调度链（spec §5.6 降级链路）。

    TODO 实施路径：app-api 侧既有 crawler 工具（走 LLM 抽取后封顶 medium 入库）
    或 agent-py 侧 requests + LLM 抽取（spec §5.6）；评估通过后替换本退化 stub。
    """
    _ = (score_date, cache)  # 退化路径暂不消费入参（保留签名契约）
    logger.warning("collect_jiuyan degraded: jiuyan 源未接入（O2 风险登记），跳过并留 data_missing")
    return []
