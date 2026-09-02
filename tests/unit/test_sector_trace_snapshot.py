from unittest.mock import AsyncMock, patch

import pytest


def _sector_row() -> dict:
    return {"pct_change": -4.2, "net_amount": -1.8e8, "lead_stock": "澜起科技", "company_num": 42}


def _fake_search_result(text: str) -> list[dict]:
    return [
        {
            "title": "存储板块暴跌原因",
            "url": "https://e.com/a",
            "content": text,
            "published_at": "2026-07-16T10:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_sector_snapshot_builds_evidence_and_facts() -> None:
    """板块快照含板块行情 market_fact + 定向检索来源，成功路径。"""
    from aistock_agent.services.sector_trace_snapshot import build_sector_snapshot

    with patch(
        "aistock_agent.services.sector_trace_snapshot._run_directed_searches",
        AsyncMock(return_value={"暴跌原因": _fake_search_result("韩检突袭存储三巨头")}),
    ):
        snap = await build_sector_snapshot(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_row=_sector_row(),
            trace_ctx=object(),
        )
    assert snap["sector"]["name"] == "存储板块"
    assert snap["sector"]["pct_change"] == -4.2
    # 定向检索来源被打平进 evidence/来源列表
    assert snap["sources"], "应含定向检索来源"


@pytest.mark.asyncio
async def test_sector_snapshot_search_failure_degrades() -> None:
    """定向搜索全部失败 → 静默降级，仍产快照（并入板块行情事实，不抛错）。"""
    from aistock_agent.services.sector_trace_snapshot import build_sector_snapshot

    with patch(
        "aistock_agent.services.sector_trace_snapshot._run_directed_searches",
        AsyncMock(return_value={}),
    ):
        snap = await build_sector_snapshot(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_row=_sector_row(),
            trace_ctx=object(),
        )
    assert snap["sector"]["pct_change"] == -4.2
    assert snap["attribution_status"] == "insufficient"
    assert "缺事件证据" in snap.get("unresolved", "")


@pytest.mark.asyncio
async def test_sector_queries_include_regulatory() -> None:
    """定向 query 覆盖监管/事件词（与大盘溯源 query 的关键区别）。"""
    from aistock_agent.services.sector_trace_snapshot import _sector_evidence_queries

    queries = _sector_evidence_queries("存储板块")
    joined = " | ".join(queries)
    assert "存储板块" in joined
    assert any(k in joined for k in ("反垄断", "调查", "监管")), "需含监管词"
