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
        )
    assert snap["sector"]["pct_change"] == -4.2
    assert snap["attribution_status"] == "insufficient"
    assert "缺事件证据" in snap.get("unresolved", "")


@pytest.mark.asyncio
async def test_sector_queries_include_regulatory() -> None:
    """定向 query 覆盖监管/事件词（与大盘溯源 query 的关键区别）。"""
    from aistock_agent.services.sector_trace_snapshot import _sector_evidence_queries

    queries = _sector_evidence_queries("存储板块", "2026-07-16")
    joined = " ".join(queries)
    assert "存储板块" in joined
    assert "2026-07-16" in joined, "query 需注入 report_date 聚焦当日"
    assert "|" not in joined, "query 不应含 |（搜索服务按字面量处理）"
    assert any(k in joined for k in ("反垄断", "调查", "监管")), "需含监管词"
    # 换词后仍要求：**每条** query 都带板块名与日期（不因分组而漏）
    for q in queries:
        assert "存储板块" in q and "2026-07-16" in q


@pytest.mark.asyncio
async def test_sector_queries_are_event_oriented_not_phenomenon() -> None:
    """换词（2026-09-18）：定向 query 由「问涨跌原因」改为**事件族**（组长口径「是原因不是现象」）。

    旧第 1 组是现象式问句 `{date} {板块} 板块 暴跌 大涨 原因` —— 检索器返回的正是
    「XX 大涨八个点」「全线上涨！涨幅第一」这类行情综述，而准入层（`is_driving_event`）
    刚写好规则专门拒它们：等于**捞回来再扔掉**，白花检索配额。现改为 5 个事件族，族间词不重叠。
    """
    from aistock_agent.services.sector_trace_snapshot import _sector_evidence_queries

    queries = _sector_evidence_queries("汽车芯片", "2026-09-18")
    joined = " ".join(queries)

    # 旧现象式问句词一律不得再出现
    for bad in ("原因", "暴跌", "大涨", "涨幅"):
        assert bad not in joined, f"现象式问句词「{bad}」不得出现在定向 query 里"

    # 5 个事件族各自至少命中一个代表词
    for kw in ("政策", "监管", "公告", "中标", "涨价", "扩产", "量产", "认证", "出口", "关税"):
        assert kw in joined, f"缺事件族词：{kw}"
    assert len(queries) == 5, "一条 query 一个事件族"


@pytest.mark.asyncio
async def test_sector_snapshot_real_search_path_returns_sources() -> None:
    """真实检索路径可用（D4.5 防断链）：不 mock _run_directed_searches，仅 mock
    TavilyService.search → sources 非空且 attribution_status == "sufficient"。"""
    from aistock_agent.services.sector_trace_snapshot import build_sector_snapshot

    with patch(
        "aistock_agent.services.sector_trace_snapshot.TavilyService.search",
        return_value={
            "results": [{"title": "x", "content": "y", "url": "https://e.com/a"}],
            "provider": "tavily",
            "outcome": "ok",
        },
    ):
        snap = await build_sector_snapshot(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_row=_sector_row(),
        )
    assert snap["sources"], "真实 Tavily 检索路径应产出来源"
    assert snap["attribution_status"] == "sufficient"
