"""板块溯源快照构建：板块行情 market_fact + 定向事件检索（Spec D · 溯源环）。

对齐大盘快照归一化约定（SourceRecord / event_evidence / market_fact）；搜索失败
静默降级（attribution_status="insufficient"），不阻断快照。定向检索直接走真实
TavilyService.search（D4.5 接线，移除不存在的 _SearchContext 抽象）。
"""

import asyncio

from aistock_agent.services.tavily import TavilyService


def _sector_evidence_queries(sector_name: str, report_date: str) -> list[str]:
    """3 组定向 query：暴跌/大涨原因、事件公告政策、监管类（命中存储狙击类）。

    注入 report_date 聚焦当日结果；中文空格连接（不用 |，搜索服务按字面量处理）。
    """
    return [
        f"{report_date} {sector_name} 板块 暴跌 大涨 原因",
        f"{report_date} {sector_name} 板块 事件 公告 政策",
        f"{report_date} {sector_name} 板块 反垄断 调查 监管",
    ]


async def _run_directed_searches(
    *, sector_name: str, report_date: str
) -> dict[str, list[dict[str, object]]]:
    """执行 3 组定向检索（真实路径：asyncio.to_thread 包 TavilyService.search）。

    返回 {query_label: [来源条目]}；失败/空结果静默 continue（保持降级语义，
    对齐大盘快照 market_trace_snapshot.py 定向搜索先例）。
    """
    results: dict[str, list[dict[str, object]]] = {}
    for q in _sector_evidence_queries(sector_name, report_date):
        try:
            search_result = await asyncio.to_thread(
                TavilyService.search, query=q, topic="news", max_results=5
            )
            if isinstance(search_result, dict):
                raw_items = search_result.get("results")
                if isinstance(raw_items, list):
                    items = [i for i in raw_items if isinstance(i, dict)]
                    if items:
                        results[q] = items
        except Exception:  # noqa: BLE001 — 定向搜索失败不影响快照主链
            continue
    return results


def _normalize_source(item: dict[str, object], *, kind: str) -> dict[str, object]:
    """来源条目归一化为大盘快照约定的形状（最小化：title/url/content/published_at）。"""
    keys = ("title", "url", "content", "published_at")
    normalized: dict[str, object] = {
        k: item.get(k) if isinstance(item, dict) else None for k in keys
    }
    normalized["kind"] = kind
    normalized["source"] = "tavily_finance_search"
    return normalized


async def build_sector_snapshot(
    *,
    report_date: str,
    sector_name: str,
    sector_row: dict[str, object] | None,
) -> dict[str, object]:
    """构建板块溯源快照。

    - sector 行情条目来自大盘快照 top_losers（pct_change/net_amount/lead_stock/company_num）
    - 定向检索走 _run_directed_searches（内部 TavilyService）；全空 → insufficient
    """
    row = sector_row or {}
    fact = {
        "type": "market_fact",
        "sector": sector_name,
        "pct_change": row.get("pct_change"),
        "net_amount": row.get("net_amount"),
        "lead_stock": row.get("lead_stock"),
        "company_num": row.get("company_num"),
    }
    searched = await _run_directed_searches(sector_name=sector_name, report_date=report_date)
    sources: list[dict[str, object]] = []
    for label, items in searched.items():
        sources.extend(_normalize_source(it, kind=f"sector_event:{label}") for it in items)
    if sources:
        return {
            "sector": {"name": sector_name, **fact},
            "sources": sources,
            "attribution_status": "sufficient",
            "missing_fields": [],
        }
    return {
        "sector": {"name": sector_name, **fact},
        "sources": [],
        "attribution_status": "insufficient",
        "missing_fields": ["sector_event_evidence"],
        "unresolved": "缺事件证据",
    }
