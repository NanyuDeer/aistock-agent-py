"""板块溯源快照构建：板块行情 market_fact + 定向事件检索（Spec D · 溯源环）。

对齐大盘快照归一化约定（SourceRecord / event_evidence / market_fact）；搜索失败
静默降级（attribution_status="insufficient"），不阻断快照。定向检索直接走真实
TavilyService.search（D4.5 接线，移除不存在的 _SearchContext 抽象）。
"""

import asyncio

from aistock_agent.services.tavily import TavilyService


def _sector_evidence_queries(sector_name: str, report_date: str) -> list[str]:
    """5 组**事件族**定向 query（2026-09-18 换词，组长口径「要溯源到基本事件——是原因，不是现象」）。

    换词前是 3 组，其中第 1 组 `{date} {板块} 板块 暴跌 大涨 原因` 是**现象式问句**：检索器返回
    的正是「XX 大涨八个点」「全线上涨！涨幅第一」这类行情综述，而准入层（`is_driving_event`）
    刚写好规则专门拒它们 —— 等于**捞回来再扔掉**，白花检索配额；第 3 组 `反垄断 调查 监管`
    是早前某次「存储狙击」案例的特化词，绝大多数日子空转。

    换词口径：
    - **一条 query 一个事件族**（政策监管 / 公司硬事件 / 供需价格 / 技术产业 / 海外贸易），
      族间词不重叠 → 提高召回多样性；族内是并列同义词（搜索服务按字面量处理，语义召回兜底）；
    - **不再出现任何现象词**（原因/暴跌/大涨/涨幅）——现象类召回一律交给准入层去拒是浪费；
    - 每族仍注入 `report_date` 聚焦当日（中文财经新闻日期写法不统一，日期 token 只是**弱锚**，
      真正提召回的是族词本身）；
    - 中文空格连接（不用 |，搜索服务按字面量处理）。
    """
    return [
        f"{report_date} {sector_name} 政策 监管 调查 部委 试点",
        f"{report_date} {sector_name} 公告 中标 订单 获批 并购",
        f"{report_date} {sector_name} 涨价 减产 扩产 供需 库存",
        f"{report_date} {sector_name} 量产 投产 认证 技术突破 招标",
        f"{report_date} {sector_name} 出口 关税 制裁 海外订单 豁免",
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
