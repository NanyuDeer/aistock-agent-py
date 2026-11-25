"""Task 7 — 大盘溯源读库优先、缺库降级测试（统一事件抓取中台）。

覆盖两件事：
1. Step 1 verbatim 回归测试（Task 1 ``load_event_scrape`` 契约）。
2. 大盘溯源（full/quick 快照）事件库优先分支：
   - 事件库有当日数据 → 直接用事件库事实做 ``event_evidence``，不调用
     telegraph/latest 直采（读库优先）；
   - 事件库空 → 完整回到原 telegraph/latest 直采路径（缺库降级，P0 功能保护）。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_store import load_event_scrape
from aistock_agent.services.market_trace_snapshot import (
    _normalize_event_store_facts,
    build_market_trace_snapshot,
    build_quick_snapshot,
)

# 复用既有快照测试的 Node 响应 fixture（tests 是包，可跨模块导入）
from tests.unit.test_market_trace_snapshot import (
    COMPLETE_CLOSE,
    GLOBAL_FACT,
    TAVILY_RESULT,
)

# Task 7 Fix Round（Minor 1）：source_id 改为 EVENT_{event_id[:19]}
# （event_id 形如 "{score_date}-{content_hash[:16]}"，[:19] = 日期 + hash 前 8 位）
EVENT_STORE_SOURCE_IDS = {
    "2026-07-19-0123456789abcdef": "EVENT_2026-07-19-01234567",
    "2026-07-19-fedcba9876543210": "EVENT_2026-07-19-fedcba98",
}

# 事件库当日数据（EventRecord 形状，Task 1 normalize_event 产物）
EVENT_STORE_EVENTS: list[dict[str, object]] = [
    {
        "event_id": "2026-07-19-0123456789abcdef",
        "title": "央行宣布降准",
        "summary": "中国人民银行决定下调存款准备金率0.5个百分点",
        "url": "https://www.cls.cn/detail/12345",
        "impact_score": 5,
        "direction": "positive",
        "involved_keywords": ["降准"],
        "source": "cls",
        "source_level": "C",
        "content_hash": "hash123",
        "scrape_at": "2026-07-19 07:00:00",
        "score_date": "2026-07-19",
        "payload": {"symbol": "600519"},
    },
    {
        "event_id": "2026-07-19-fedcba9876543210",
        "title": "美联储维持利率不变",
        "summary": "美联储在最新议息会议上决定维持联邦基金利率目标区间不变",
        "url": "https://example.com/fed",
        "impact_score": 4,
        "direction": "neutral",
        "involved_keywords": [],
        "source": "tavily",
        "source_level": "B",
        "content_hash": "hash456",
        "scrape_at": "2026-07-19 06:30:00",
        "score_date": "2026-07-19",
        "payload": {"symbol": "000001"},
    },
]


@pytest.mark.asyncio
async def test_review_can_consume_event_store():
    with patch("aistock_agent.services.event_store.node_api") as mock_api:
        # 现状适配（记录偏差）：Task 1 的 load_event_scrape 走 get_analysis_report
        # （内部再调 node_api.get）。直接 patch .get 不可达——MagicMock 对
        # get_analysis_report 自动创建子 mock，await 抛 TypeError 被吞后返回 []。
        # M2 修复后 load_event_scrape 改走 get_analysis_report_quiet（404 静默），
        # patch 目标同步更新。
        mock_api.get_analysis_report_quiet = AsyncMock(
            return_value={"content": {"events": [{"event_id": "e1", "title": "宏观事件"}]}}
        )
        events = await load_event_scrape("2026-08-12")
        assert events


# ============================================================================
# 大盘溯源：事件库优先（读库优先）
# ============================================================================


@pytest.mark.asyncio
async def test_build_market_trace_prefers_event_store_over_telegraph(mocker):
    """事件库有当日数据时：full 快照直接用事件库，telegraph/latest 直采不被调用。"""
    node_get_calls: list[str] = []

    async def _node_get_side_effect(path: str, **_kwargs):
        node_get_calls.append(path)
        # 三期：close-snapshot 调用带 ?date={report_date}，必须用 startswith 匹配
        if path.startswith("/internal/market/close-snapshot"):
            return COMPLETE_CLOSE
        return {"items": []}

    mocker.patch.object(node_api, "get", side_effect=_node_get_side_effect)
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[GLOBAL_FACT]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value=TAVILY_RESULT,
    )
    mocker.patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=EVENT_STORE_EVENTS),
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    # 事件库事实进入 sources（EVENT_{event_id[:19]} 前缀 + 事件库字段，Fix Round Minor 1）
    first_id = EVENT_STORE_SOURCE_IDS["2026-07-19-0123456789abcdef"]
    second_id = EVENT_STORE_SOURCE_IDS["2026-07-19-fedcba9876543210"]
    assert first_id in snapshot.sources
    event_fact = snapshot.sources[first_id]
    assert event_fact.kind == "event_evidence"
    assert event_fact.title == "央行宣布降准"
    assert event_fact.provider == "cls"
    assert event_fact.url == "https://www.cls.cn/detail/12345"
    assert event_fact.content == "中国人民银行决定下调存款准备金率0.5个百分点"
    # 事件库 source_level C → review 的 reporting 档（A→primary）
    assert event_fact.source_level == "reporting"
    assert event_fact.occurred_at is not None
    # 两条事件库事实都在（event_id 切片唯一，不互相覆盖）
    assert snapshot.sources[second_id].title == "美联储维持利率不变"
    assert snapshot.sources[second_id].provider == "tavily"
    # 状态标记为事件库命中
    assert snapshot.collection_status["cls_news"].provider == "event_store"
    assert snapshot.collection_status["cls_news"].state == "available"
    # 读库优先：不调用任何 /internal/news/ 直采
    assert not any("/internal/news/" in c for c in node_get_calls), (
        f"事件库命中后不应直采，实际调用: {node_get_calls}"
    )


@pytest.mark.asyncio
async def test_build_quick_snapshot_prefers_event_store(mocker):
    """事件库有当日数据时：quick 快照同样直接使用事件库。"""
    node_get_calls: list[str] = []

    async def _node_get_side_effect(path: str, **_kwargs):
        node_get_calls.append(path)
        return {"items": []}

    mocker.patch.object(
        node_api, "get_quick_snapshot", AsyncMock(return_value=COMPLETE_CLOSE)
    )
    mocker.patch.object(node_api, "get", side_effect=_node_get_side_effect)
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    mocker.patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=EVENT_STORE_EVENTS),
    )

    snapshot = await build_quick_snapshot("2026-07-19")

    first_id = EVENT_STORE_SOURCE_IDS["2026-07-19-0123456789abcdef"]
    second_id = EVENT_STORE_SOURCE_IDS["2026-07-19-fedcba9876543210"]
    assert first_id in snapshot.sources
    assert snapshot.sources[first_id].kind == "event_evidence"
    assert snapshot.sources[first_id].title == "央行宣布降准"
    assert snapshot.sources[second_id].provider == "tavily"
    assert snapshot.collection_status["cls_news"].provider == "event_store"
    # 读库优先：quick 快照同样不调用 telegraph/latest 直采
    assert not any("/internal/news/" in c for c in node_get_calls), (
        f"事件库命中后不应直采，实际调用: {node_get_calls}"
    )


# ============================================================================
# 大盘溯源：缺库降级（必须完整回到原直采路径）
# ============================================================================


@pytest.mark.asyncio
async def test_build_market_trace_falls_back_to_telegraph_when_event_store_empty(mocker):
    """事件库空（当日无抓取事件）时：完整回到原 telegraph/latest 直采。"""
    node_get_calls: list[str] = []

    async def _node_get_side_effect(path: str, **_kwargs):
        node_get_calls.append(path)
        # close-snapshot 与电报/晨报读取统一返回 COMPLETE_CLOSE
        # （COMPLETE_CLOSE 内含 items，直采时能归一化出 NEWS_001）
        return COMPLETE_CLOSE

    mocker.patch.object(node_api, "get", side_effect=_node_get_side_effect)
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    # 缺库：事件库返回空列表
    mocker.patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(return_value=[]),
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    # 降级后回到原直采：telegraph 被调用，NEWS_ 系列来源照常生成
    assert any("/internal/news/" in c for c in node_get_calls), (
        f"缺库时应走直采，实际调用: {node_get_calls}"
    )
    assert EVENT_STORE_SOURCE_IDS["2026-07-19-0123456789abcdef"] not in snapshot.sources
    assert snapshot.sources["NEWS_001"].kind == "event_evidence"
    assert snapshot.collection_status["cls_news"].provider == "cls"


@pytest.mark.asyncio
async def test_build_market_trace_falls_back_when_event_store_read_fails(mocker):
    """事件库读取抛异常（load_event_scrape 内部吞掉返回 []）时同样降级直采。"""
    node_get_calls: list[str] = []

    async def _node_get_side_effect(path: str, **_kwargs):
        node_get_calls.append(path)
        # 同前：统一返回 COMPLETE_CLOSE（含 items，直采可归一化出 NEWS_001）
        return COMPLETE_CLOSE

    mocker.patch.object(node_api, "get", side_effect=_node_get_side_effect)
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    mocker.patch(
        "aistock_agent.services.event_store.load_event_scrape",
        new=AsyncMock(side_effect=RuntimeError("node down")),
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    assert any("/internal/news/" in c for c in node_get_calls)
    assert EVENT_STORE_SOURCE_IDS["2026-07-19-0123456789abcdef"] not in snapshot.sources
    assert snapshot.sources["NEWS_001"].kind == "event_evidence"


# ============================================================================
# Task 7 Fix Round（Important 1）：事件非空但全部无 URL → invalid_for_causality
# （与 _normalize_news_facts 语义对齐，防御性一致补强）
# ============================================================================


def test_normalize_event_store_facts_all_missing_url_invalid_for_causality():
    """事件非空但全部无 URL：返回 invalid_for_causality 并补 missing_fields。

    不再误报 available（与 _normalize_news_facts 语义对齐，Fix Round Important 1）。
    """
    from datetime import datetime

    url_less_events = [
        {**ev, "url": ""} for ev in EVENT_STORE_EVENTS
    ]
    sources: dict[str, object] = {}
    missing_fields: list[str] = []

    status = _normalize_event_store_facts(
        url_less_events,
        sources,
        missing_fields,
        captured_at=datetime(2026, 7, 19, 15, 30),
    )

    assert status.state == "invalid_for_causality"
    assert status.provider == "event_store"
    assert status.item_count == 2
    assert "cls_news" in missing_fields
    # 与 _normalize_news_facts 一致：facts 已写入 sources，但状态标记不可用
    assert EVENT_STORE_SOURCE_IDS["2026-07-19-0123456789abcdef"] in sources
    assert EVENT_STORE_SOURCE_IDS["2026-07-19-fedcba9876543210"] in sources
