from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.services.event_scraper import (
    run_event_scrape,
    scrape_event_triggered,
    scrape_full_daily,
    scrape_intraday,
)
from aistock_agent.services.event_store import is_major_event, normalize_event


@pytest.mark.asyncio
async def test_run_event_scrape_full_daily():
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_ths_original",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_tavily",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_global_markets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 0,
                "deduped": 0,
                "added": 0,
                "added_events": [],
                "error": None,
            }
        ),
    ):
        result = await run_event_scrape("full_daily", score_date="2026-08-12")
    assert result["scrape_mode"] == "full_daily"
    assert result["persisted"] == 0


@pytest.mark.asyncio
async def test_run_event_scrape_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown scrape_mode"):
        await run_event_scrape("unknown_mode", score_date="2026-08-12")


def _make_event(*, title: str, impact_score: int, **extra: object):
    """构造归一化事件（normalize_event 兜底 symbol/involved_keywords 进 payload）。"""
    raw: dict[str, object] = {"title": title, "impact_score": impact_score}
    raw.update(extra)
    ev = normalize_event(raw, source="eastmoney", score_date="2026-08-12")
    assert ev is not None
    return ev


@pytest.mark.asyncio
async def test_scrape_intraday_only_persists_major_events():
    """intraday 分支：电报+东财含重大/普通事件 → 仅重大事件（impact_score>=4）落库。"""
    major = _make_event(title="重大公告", impact_score=5)
    normal = _make_event(title="普通公告", impact_score=1)
    save = AsyncMock(
        return_value={
            "persisted": 1,
            "deduped": 0,
            "added": 1,
            "added_events": [major],
            "error": None,
        }
    )
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[normal]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[major]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=save,
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        await scrape_intraday("2026-08-12")
    saved = save.await_args.args[0]
    assert len(saved) == 1
    assert saved[0]["impact_score"] == 5
    assert all(is_major_event(ev) for ev in saved)
    # Task 5 + I3：入库有新增（added>0）且有重大事件 → fire-and-forget 触发传导，
    # 且只传新增子集（added_events）
    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.args[0][0]["title"] == "重大公告"


@pytest.mark.asyncio
async def test_scrape_intraday_skips_conduction_when_all_deduped():
    """I3：全去重批次（persisted>0 但 added=0）不重复触发传导（LLM 成本）。"""
    major = _make_event(title="盘中异动公告", impact_score=5)
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[major]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 10,
                "deduped": 1,
                "added": 0,
                "added_events": [],
                "error": None,
            }
        ),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        await scrape_intraday("2026-08-12")
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_event_triggered_persists_all_evidence_unfiltered():
    """event_triggered 分支：普通事件（impact_score=1）仍全量落库（用户裁决豁免筛选）。"""
    by_symbol = _make_event(title="异动公告", impact_score=1, symbol="600000")
    by_keyword = _make_event(
        title="异动研报", impact_score=1, involved_keywords=["600000 涨停"]
    )
    other = _make_event(title="别家公告", impact_score=5, symbol="000001")
    save = AsyncMock(
        return_value={
            "persisted": 2,
            "deduped": 0,
            "added": 2,
            "added_events": [by_symbol, by_keyword],
            "error": None,
        }
    )
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[by_symbol, by_keyword, other]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=save,
    ):
        await scrape_event_triggered({"symbol": "600000", "score_date": "2026-08-12"})
    saved = save.await_args.args[0]
    assert len(saved) == 2  # 仅本标的关联事件保留（payload.symbol / involved_keywords 双匹配）
    assert all(ev["impact_score"] == 1 for ev in saved)  # 普通事件不被 is_major_event 过滤
    assert all(not is_major_event(ev) for ev in saved)


@pytest.mark.asyncio
async def test_scrape_event_triggered_requires_symbol():
    """M5：symbol 为空时返回错误且不采集/不落库（避免全量东财事件污染当日事件库）。"""
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[_make_event(title="任意东财事件", impact_score=1)]),
    ) as mock_collect, patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(),
    ) as mock_save:
        result = await scrape_event_triggered({})
    assert result == {
        "persisted": 0,
        "deduped": 0,
        "added": 0,
        "added_events": [],
        "error": "symbol required",
    }
    mock_collect.assert_not_awaited()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_scrape_full_daily_applies_llm_scores_when_enabled():
    """开关开启时：评分在重大筛选前应用，LLM 评分结果影响入库。"""
    with patch("aistock_agent.config.settings.event_scoring_llm_enabled", True), \
         patch("aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_ths_original",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_tavily",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_global_markets",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scraper.event_scoring_llm.score_events_llm",
               new=AsyncMock(side_effect=lambda events, **kwargs: events)) as mock_score, \
         patch("aistock_agent.services.event_store.save_event_scrape",
               new=AsyncMock(return_value={"persisted": 0, "deduped": 0, "added": 0, "added_events": [], "error": None})):
        await scrape_full_daily("2026-08-13")
    mock_score.assert_awaited_once()


@pytest.mark.asyncio
async def test_scrape_full_daily_skips_llm_scores_when_disabled():
    """开关默认关闭：零 LLM 评分调用（行为与现状一致）。"""
    with patch("aistock_agent.config.settings.event_scoring_llm_enabled", False), \
         patch("aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_ths_original",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_tavily",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_global_markets",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scraper.event_scoring_llm.score_events_llm",
               new=AsyncMock()) as mock_score, \
         patch("aistock_agent.services.event_store.save_event_scrape",
               new=AsyncMock(return_value={"persisted": 0, "deduped": 0, "added": 0, "added_events": [], "error": None})):
        await scrape_full_daily("2026-08-13")
    mock_score.assert_not_awaited()


@pytest.mark.asyncio
async def test_scrape_intraday_applies_llm_scores_when_enabled():
    with patch("aistock_agent.config.settings.event_scoring_llm_enabled", True), \
         patch("aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
               new=AsyncMock(return_value=[])), \
         patch("aistock_agent.services.event_scraper.event_scoring_llm.score_events_llm",
               new=AsyncMock(side_effect=lambda events, **kwargs: events)) as mock_score, \
         patch("aistock_agent.services.event_store.save_event_scrape",
               new=AsyncMock(return_value={"persisted": 0, "deduped": 0, "added": 0, "added_events": [], "error": None})):
        await scrape_intraday("2026-08-13")
    mock_score.assert_awaited_once()
