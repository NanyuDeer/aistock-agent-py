"""板块溯源快照构建：板块行情 market_fact + 定向事件检索（Spec D · 溯源环）。

对齐大盘快照归一化约定（SourceRecord / event_evidence / market_fact）；搜索失败
静默降级（attribution_status="insufficient"），不阻断快照。
"""

from typing import Protocol


class _SearchContext(Protocol):
    """定向检索上下文：ctx.search(q, date=...) 对齐大盘快照检索接口。"""

    async def search(self, query: str, *, date: str) -> object: ...


def _sector_evidence_queries(sector_name: str) -> list[str]:
    """3 组定向 query：暴跌/大涨原因、事件|公告|政策、监管类（命中存储狙击类）。"""
    return [
        f"{sector_name} 暴跌|大涨 原因",
        f"{sector_name} 事件|公告|政策",
        f"{sector_name} 反垄断|调查|监管",
    ]


async def _run_directed_searches(
    *, sector_name: str, trade_date: str, ctx: _SearchContext
) -> dict[str, list[dict[str, object]]]:
    """执行 3 组定向检索，返回 {query_label: [来源条目]}；失败静默返回空（调用方可降级）。"""
    results: dict[str, list[dict[str, object]]] = {}
    for q in _sector_evidence_queries(sector_name):
        try:
            items = await ctx.search(q, date=trade_date)  # ctx.search 对齐大盘快照检索接口
            if isinstance(items, list):
                results[q] = [i for i in items if isinstance(i, dict)]
        except Exception:
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
    trace_ctx: _SearchContext,
) -> dict[str, object]:
    """构建板块溯源快照。

    - sector 行情条目来自大盘快照 top_losers（pct_change/net_amount/lead_stock/company_num）
    - 定向检索来自 _run_directed_searches；全空 → attribution_status="insufficient"
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
    searched = await _run_directed_searches(
        sector_name=sector_name, trade_date=report_date, ctx=trace_ctx
    )
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
