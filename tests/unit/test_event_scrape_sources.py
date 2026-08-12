from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_scrape_sources import (
    collect_cls_telegraph,
    collect_eastmoney_judgements,
    collect_global_markets,
    collect_tavily,
    collect_ths_original,
)
from aistock_agent.tools.market_tools import GlobalMarketFetchError
from aistock_agent.utils.date import shanghai_today

_TODAY = shanghai_today().isoformat()


@pytest.mark.asyncio
async def test_collect_cls_telegraph_normalizes_items():
    raw_items = [
        {
            "id": "1001",
            "title": "央行降准",
            "content": "央行宣布降准0.5个百分点",
            "time": "2026-08-12 09:30:00",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": raw_items})
        events = await collect_cls_telegraph("2026-08-12")
        assert len(events) == 1
        assert events[0]["title"] == "央行降准"
        assert events[0]["source"] == "cls"
        assert events[0]["url"] == "https://www.cls.cn/detail/1001"


@pytest.mark.asyncio
async def test_collect_cls_telegraph_falls_back_to_latest():
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        async def fake_get(path, **kwargs):
            if "telegraph" in path:
                raise RuntimeError("telegraph failed")
            return {"items": [{"id": "2001", "title": "降级事件", "content": "x"}]}

        mock_api.get = AsyncMock(side_effect=fake_get)
        events = await collect_cls_telegraph("2026-08-12")
        assert len(events) == 1
        assert events[0]["title"] == "降级事件"


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_reads_existing_table():
    """C1：Node getEvents 返回 {total, events}（键名 events 非 items），真实键名可读。"""
    rows = [
        {
            "title": "某公司重大资产重组",
            "ai_summary": "重组预案披露",
            "ai_impact": "重大利好",
            "symbol": "600000",
            "published_at": f"{_TODAY} 09:30:00",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"total": 1, "events": rows})
        events = await collect_eastmoney_judgements(_TODAY)
        assert len(events) == 1
        assert events[0]["source"] == "eastmoney"
        # 真实键名：node_api.get 收到 alerts 请求（回归保护）
        mock_api.get.assert_awaited_once_with("/internal/monitor/alerts?days=1")


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_reads_legacy_items_key():
    """兼容兜底：若接口返回 items 键（历史响应）也不炸，返回空列表。"""
    rows = [{"title": "历史事件", "published_at": f"{_TODAY} 10:00:00"}]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_eastmoney_judgements(_TODAY)
        assert events == []


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_filters_stale_rows_by_date():
    """I1：跨日陈旧行（昨日/前日 published_at）被过滤，仅保留当日行。"""
    today_rows = [
        {
            "title": "当日事件",
            "ai_summary": "x",
            "published_at": f"{_TODAY} 10:00:00",
        },
        {
            "title": "当日事件T格式",
            "ai_summary": "x",
            "event_time": f"{_TODAY}T10:00:00",
        },
    ]
    stale_rows = [
        {"title": "昨日事件", "ai_summary": "x", "published_at": "2026-08-11 23:00:00"},
        {"title": "前日事件", "ai_summary": "x", "event_time": "2026-08-10T20:00:00"},
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(
            return_value={"total": 4, "events": today_rows + stale_rows}
        )
        events = await collect_eastmoney_judgements(_TODAY)
    titles = {ev["title"] for ev in events}
    assert titles == {"当日事件", "当日事件T格式"}
    assert all(ev["score_date"] == _TODAY for ev in events)


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_keeps_rows_without_time():
    """I1 边界：行无时间字段（published_at/event_time 均缺）时保守保留。"""
    rows = [{"title": "无时间字段事件", "ai_summary": "x"}]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"events": rows})
        events = await collect_eastmoney_judgements(_TODAY)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_utc_maps_to_shanghai_date():
    """I1 时区精度：Node UTC ISO（toISOString 强制 UTC）按上海时区日期归属过滤。

    - 当日 UTC 02:00（上海当日 10:00，UTC 日期 == score_date）→ 保留
    - 前一日 UTC 22:00（上海当日 06:00，北京 00:00-07:59 凌晨事件；修复前
      startswith(score_date) 按 UTC 日期前缀会把当日事件误过滤）→ 保留
    - 前一日 UTC 10:00（上海前一日 18:00，真陈旧行）→ 过滤
    """
    today = shanghai_today()
    today_str = today.isoformat()
    yesterday_str = (today - timedelta(days=1)).isoformat()
    rows = [
        {
            "title": "UTC当日事件",
            "ai_summary": "x",
            "published_at": f"{today_str}T02:00:00.000Z",
        },
        {
            "title": "北京凌晨当日事件",
            "ai_summary": "x",
            "published_at": f"{yesterday_str}T22:00:00.000Z",
        },
        {
            "title": "昨日陈旧事件",
            "ai_summary": "x",
            "published_at": f"{yesterday_str}T10:00:00.000Z",
        },
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"total": 3, "events": rows})
        events = await collect_eastmoney_judgements(today_str)
    titles = {ev["title"] for ev in events}
    assert titles == {"UTC当日事件", "北京凌晨当日事件"}


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_explicit_utc_offset_converted():
    """Min-1：非 Z 但带显式偏移（+00:00）的字符串按原偏移换算上海墙钟。

    "2026-08-11T18:00:00+00:00" = UTC 18:00 = 上海 2026-08-12 02:00，
    score_date=2026-08-12 当日事件必须保留；修复前 replace(tzinfo=上海)
    覆盖原偏移不换算，被当作上海 8-11 18:00 误过滤为陈旧行。
    """
    rows = [
        {
            "title": "UTC偏移当日事件",
            "ai_summary": "x",
            "published_at": "2026-08-11T18:00:00+00:00",
        },
        {
            "title": "UTC偏移昨日陈旧事件",
            "ai_summary": "x",
            "published_at": "2026-08-11T10:00:00+00:00",  # = 上海 8-11 18:00，真陈旧
        },
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"total": 2, "events": rows})
        events = await collect_eastmoney_judgements("2026-08-12")
    titles = {ev["title"] for ev in events}
    assert titles == {"UTC偏移当日事件"}


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_unparseable_time_filtered():
    """I1 边界：无法解析的时间字符串宽容回退（取前 10 字符比较），不崩溃。"""
    rows = [{"title": "异常时间事件", "ai_summary": "x", "published_at": "not-a-date"}]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"events": rows})
        events = await collect_eastmoney_judgements(_TODAY)
    assert events == []


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_maps_detail_url():
    """I2：Node mapJudgementToEvent 输出 detail_url（非 url），归一化后 url 有值。"""
    rows = [
        {
            "title": "带详情链接的事件",
            "ai_summary": "x",
            "detail_url": "https://em.example.com/detail/123",
            "published_at": f"{_TODAY} 10:00:00",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"total": 1, "events": rows})
        events = await collect_eastmoney_judgements(_TODAY)
    assert len(events) == 1
    assert events[0]["url"] == "https://em.example.com/detail/123"


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_fails_returns_empty():
    """异常降级：node_api.get 抛异常 → 返回 []（采集层永不 500）。"""
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node down"))
        events = await collect_eastmoney_judgements(_TODAY)
        assert events == []


@pytest.mark.asyncio
async def test_collect_ths_original_normalizes_items():
    """正常路径：items → 归一化 EventRecord（source=ths_original）。"""
    rows = [
        {
            "source_id": "s1",
            "title": "同花顺原创标题",
            "content": "正文内容",
            "keywords": ["半导体", "靶材"],
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_ths_original("2026-08-12")
        assert len(events) == 1
        assert events[0]["source"] == "ths_original"
        assert events[0]["summary"] == "正文内容"
        assert events[0]["involved_keywords"] == ["半导体", "靶材"]


@pytest.mark.asyncio
async def test_collect_ths_original_handles_json_string_keywords():
    """Min-1：keywords 为 JSONB，Node 可能返回 JSON 字符串，防御解析；失败 → []。"""
    rows = [
        {"title": "A", "content": "x", "keywords": '["半导体", "靶材"]'},
        {"title": "B", "content": "y", "keywords": "not-json"},
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_ths_original("2026-08-12")
        assert len(events) == 2
        assert events[0]["involved_keywords"] == ["半导体", "靶材"]
        assert events[1]["involved_keywords"] == []


@pytest.mark.asyncio
async def test_collect_ths_original_fails_returns_empty():
    """异常降级：node_api.get 抛异常 → 返回 []。"""
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node down"))
        events = await collect_ths_original("2026-08-12")
        assert events == []


class _FakeTavily:
    @staticmethod
    def search(query: str, **kwargs: object) -> dict[str, object]:
        return {
            "results": [
                {"title": "政策利好", "content": "摘要", "url": "https://tavily.com/1"}
            ]
        }


class _FakeTavilyFail:
    @staticmethod
    def search(query: str, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("tavily down")


@pytest.mark.asyncio
async def test_collect_tavily_normalizes_results():
    """正常路径：TavilyService.search 返回 results → 归一化（source=tavily）。"""
    # collect_tavily 函数内 from-import TavilyService，patch 源模块 tavily.TavilyService
    with patch("aistock_agent.services.tavily.TavilyService", new=_FakeTavily):
        events = await collect_tavily("2026-08-12")
    assert len(events) == 2  # 两个 query 各返回一条（含去重前同 content_hash）
    assert all(ev["source"] == "tavily" for ev in events)
    assert events[0]["title"] == "政策利好"
    assert events[0]["url"] == "https://tavily.com/1"


@pytest.mark.asyncio
async def test_collect_tavily_search_fails_returns_empty():
    """异常降级：search 抛异常 → 返回 []（逐 query 捕获 continue）。"""
    with patch("aistock_agent.services.tavily.TavilyService", new=_FakeTavilyFail):
        events = await collect_tavily("2026-08-12")
    assert events == []


@pytest.mark.asyncio
async def test_collect_global_markets_normalizes_facts():
    """正常路径：结构化事实 → 归一化（source=global_markets，>=1% 波动 impact_score=5）。"""
    facts = [
        {"ticker": "NDX", "name": "纳斯达克", "price": 21000.0, "change_pct": 1.5},
        {"ticker": "HSI", "name": "恒生指数", "price": 22000.0, "change_pct": 0.5},
        # Min-5：name/ticker 均为空 → 跳过
        {"ticker": "", "name": "", "price": 1.0, "change_pct": 2.0},
    ]
    # collect_global_markets 函数内 from-import collect_global_market_facts，
    # patch 源模块 market_tools.collect_global_market_facts
    with patch(
        "aistock_agent.tools.market_tools.collect_global_market_facts",
        new=AsyncMock(return_value=facts),
    ):
        events = await collect_global_markets()
    assert len(events) == 2
    assert all(ev["source"] == "global_markets" for ev in events)
    assert events[0]["impact_score"] == 5  # |1.5| >= 1 记为重大事实
    assert events[1]["impact_score"] == 1  # |0.5| < 1 普通事实


@pytest.mark.asyncio
async def test_collect_global_markets_fails_returns_empty():
    """异常降级：collect_global_market_facts 抛 GlobalMarketFetchError → 返回 []。"""
    with patch(
        "aistock_agent.tools.market_tools.collect_global_market_facts",
        new=AsyncMock(side_effect=GlobalMarketFetchError("node down")),
    ):
        events = await collect_global_markets()
    assert events == []


@pytest.mark.asyncio
async def test_collect_cls_telegraph_scores_major_event():
    """P0-1：cls 电报标题命中强事件词 → impact_score 5（过阈入库）。"""
    raw_items = [
        {
            "id": "3001",
            "title": "央行宣布降准0.5个百分点",
            "content": "央行降准",
            "time": "2026-08-12 09:30:00",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": raw_items})
        events = await collect_cls_telegraph("2026-08-12")
    assert len(events) == 1
    assert events[0]["impact_score"] == 5
    assert events[0]["direction"] == "positive"


@pytest.mark.asyncio
async def test_collect_ths_original_scores_by_content():
    """P0-1：ths 内容命中强负面词 → impact_score 5。"""
    rows = [
        {
            "source_id": "s1",
            "title": "某某公司公告",
            "content": "控股股东拟减持不超过2%股份",
            "keywords": [],
            "published_at": "2026-08-12T02:00:00.000Z",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_ths_original("2026-08-12")
    assert len(events) == 1
    assert events[0]["impact_score"] == 5
    assert events[0]["direction"] == "negative"


@pytest.mark.asyncio
async def test_collect_tavily_neutral_scores_1():
    """P0-1：tavily 无命中词 → impact_score 1（不过阈不入库，维持过滤面）。"""
    with patch(
        "aistock_agent.services.event_scrape_sources.asyncio.to_thread",
        new=AsyncMock(
            return_value={"results": [{"title": "市场综述", "content": "今日两市震荡", "url": "https://t/1"}]}
        ),
    ):
        events = await collect_tavily("2026-08-12")
    # 注：collect_tavily 遍历 2 个 query，各返回一条相同事件（同 content_hash，
    # 同批内不去重，去重在落库阶段）→ 2 条，均应为中性 1 分
    assert len(events) == 2
    assert all(ev["impact_score"] == 1 for ev in events)
