"""搜索 provider 观测计数单测 — MetricsCollector search 块

验证：
- record_search_attempt / record_search_failed / record_search_budget_exhausted /
  record_search_empty 累加进 get_metrics()["search"] 快照
- reset() 清空 search 块
- /metrics 端点（get_stock_trace_observability）暴露 search 顶层键
"""

from __future__ import annotations

import pytest


def test_search_metrics_accumulate_and_snapshot():
    from aistock_agent.observability.metrics import MetricsCollector

    c = MetricsCollector()
    c.record_search_attempt("anysearch")
    c.record_search_attempt("tavily")
    c.record_search_attempt("anysearch")
    c.record_search_failed("tavily")
    c.record_search_budget_exhausted()
    c.record_search_empty()

    m = c.get_metrics()["search"]
    assert m["attempts"]["anysearch"] == 2
    assert m["attempts"]["tavily"] == 1
    assert m["failed"]["tavily"] == 1
    assert m["budget_exhausted"] == 1
    assert m["empty"] == 1


def test_search_metrics_reset():
    from aistock_agent.observability.metrics import MetricsCollector

    c = MetricsCollector()
    c.record_search_attempt("anysearch")
    c.reset()
    m = c.get_metrics()["search"]
    assert m["attempts"] == {}
    assert m["failed"] == {}
    assert m["budget_exhausted"] == 0
    assert m["empty"] == 0


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_search_block() -> None:
    """/metrics 观测：search 块（attempts/failed/budget_exhausted/empty）暴露。"""
    from aistock_agent.api.routes import get_stock_trace_observability
    from aistock_agent.observability.metrics import get_metrics_collector

    collector = get_metrics_collector()
    collector.reset()
    collector.record_search_attempt("anysearch")
    collector.record_search_failed("tavily")

    snap = await get_stock_trace_observability()
    search = snap["search"]
    assert search["attempts"]["anysearch"] == 1
    assert search["failed"]["tavily"] == 1
    assert search["budget_exhausted"] == 0
    assert search["empty"] == 0
