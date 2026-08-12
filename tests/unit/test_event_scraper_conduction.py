"""event_scraper 传导触发测试 — 中台入库成功后触发事件分析流水线（Task 5）。

覆盖：
- `_trigger_conduction`：事件列表 → major_events 映射 → 调用 run_event_analysis_pipeline；
  空列表不触发；流水线异常不向上抛（fire-and-forget 语义）
- `_spawn_conduction`：fire-and-forget 包装，持有 task 强引用（防 GC，对齐 morning
  时代 scheduler._pending_event_tasks 先例），完成后自动从集合移除
- `scrape_full_daily` / `scrape_intraday`：落库成功（persisted>0）且有重大事件时才
  触发 `_spawn_conduction`，不改变返回契约
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.services.event_scraper import _spawn_conduction, _trigger_conduction
from aistock_agent.services.event_store import normalize_event
from aistock_agent.utils.date import shanghai_today

# patch 目标说明：`_trigger_conduction` 内函数级 `from event_analysis_pipeline import
# run_event_analysis_pipeline`（运行期从源模块取属性），必须 patch 源模块属性
# （from-import 绑定陷阱，对齐 event_scraper 模块 docstring 备注）。
_PIPELINE_PATH = "aistock_agent.services.event_analysis_pipeline.run_event_analysis_pipeline"


def _make_event(*, title: str, impact_score: int, **extra: object) -> dict[str, object]:
    """构造归一化 EventRecord（normalize_event 兜底 summary/url/direction 等默认值）。"""
    raw: dict[str, object] = {"title": title, "impact_score": impact_score}
    raw.update(extra)
    ev = normalize_event(raw, source="eastmoney", score_date="2026-08-12")
    assert ev is not None
    return dict(ev)


# ── _trigger_conduction ──


@pytest.mark.asyncio
async def test_trigger_conduction_calls_pipeline():
    events = [
        {
            "event_id": "e1",
            "title": "央行降准",
            "summary": "降准0.5个百分点",
            "url": "https://example.com/1",
            "impact_score": 5,
            "direction": "positive",
            "involved_keywords": ["银行"],
            "source": "cls",
            "source_level": "A",
            "content_hash": "abc",
            "scrape_at": "2026-08-12 10:00:00",
            "score_date": "2026-08-12",
            "payload": {},
        }
    ]
    with patch(
        _PIPELINE_PATH,
        new=AsyncMock(return_value={"event_count": 1}),
    ) as mock_pipeline:
        await _trigger_conduction(events)
        mock_pipeline.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_conduction_maps_major_event_fields():
    """EventRecord → major_events 字段映射（event_id/title/summary/url/impact_score/
    direction/involved_keywords），多余字段不传给流水线。"""
    events = [
        _make_event(
            title="美联储加息",
            impact_score=5,
            summary="加息25bp",
            url="https://example.com/fed",
            direction="negative",
            involved_keywords=["美股", "美元"],
            source_level="A",
        )
    ]
    with patch(
        _PIPELINE_PATH,
        new=AsyncMock(return_value={"event_count": 1}),
    ) as mock_pipeline:
        await _trigger_conduction(events)
    called = mock_pipeline.await_args.args[0]
    assert len(called) == 1
    assert called[0] == {
        "event_id": events[0]["event_id"],
        "title": "美联储加息",
        "summary": "加息25bp",
        "url": "https://example.com/fed",
        "impact_score": 5,
        "direction": "negative",
        "involved_keywords": ["美股", "美元"],
    }


@pytest.mark.asyncio
async def test_trigger_conduction_skips_empty_events():
    """空列表直接返回，不调用流水线。"""
    with patch(
        _PIPELINE_PATH,
        new=AsyncMock(),
    ) as mock_pipeline:
        await _trigger_conduction([])
        mock_pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_conduction_survives_pipeline_exception():
    """流水线抛异常被捕获记日志，不向上抛（fire-and-forget 失败不阻断）。"""
    events = [_make_event(title="重大事件", impact_score=5)]
    with patch(
        _PIPELINE_PATH,
        new=AsyncMock(side_effect=RuntimeError("LLM 不可用")),
    ):
        # 不抛异常即通过
        await _trigger_conduction(events)


# ── _spawn_conduction ──


@pytest.mark.asyncio
async def test_spawn_conduction_keeps_reference_and_discards_on_done():
    """_spawn_conduction 持有 task 强引用（防 GC 提前取消），完成后自动移除。

    模块级集合可能含其他测试残留的未完成 task（不同 event loop），
    断言基于快照差值，只观察本次创建的 task。
    """
    from aistock_agent.services import event_scraper as scraper_module

    before = set(scraper_module._pending_conduction_tasks)
    events = [_make_event(title="传导事件", impact_score=5)]
    with patch(
        _PIPELINE_PATH,
        new=AsyncMock(return_value={"event_count": 1}),
    ):
        _spawn_conduction(events)
        spawned = scraper_module._pending_conduction_tasks - before
        assert len(spawned) == 1
        task = next(iter(spawned))
        await task
    assert len(scraper_module._pending_conduction_tasks - before) == 0


# ── scrape_full_daily / scrape_intraday 触发条件 ──


@pytest.mark.asyncio
async def test_scrape_full_daily_triggers_conduction_when_persisted():
    """full_daily：有重大事件且本批有新增（added>0）→ 触发 _spawn_conduction。

    score_date 用 shanghai_today() 动态计算：collect_global_markets 仅在
    score_date == _today() 时才被采集（event_scraper.py:86-87），硬编码日期
    会在非当天运行必然失败（Task 5 评审 Important 1 日期依赖时间炸弹）。
    """
    today = shanghai_today().isoformat()
    major = _make_event(title="盘前重磅", impact_score=5)
    normal = _make_event(title="普通公告", impact_score=1)
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
        new=AsyncMock(return_value=[normal]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_global_markets",
        new=AsyncMock(return_value=[major]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 1,
                "deduped": 0,
                "added": 1,
                "added_events": [major],
                "error": None,
            }
        ),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        from aistock_agent.services.event_scraper import scrape_full_daily

        result = await scrape_full_daily(today)

    assert result["persisted"] == 1
    mock_spawn.assert_called_once()
    # 仅重大事件（impact_score>=4）传入触发，且只传新增子集（I3）
    passed = mock_spawn.call_args.args[0]
    assert len(passed) == 1
    assert passed[0]["title"] == "盘前重磅"


@pytest.mark.asyncio
async def test_scrape_full_daily_skips_conduction_when_nothing_persisted():
    """full_daily：落库失败/未新增（added=0）→ 不触发传导。"""
    today = shanghai_today().isoformat()
    major = _make_event(title="盘前重磅", impact_score=5)
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
        new=AsyncMock(return_value=[major]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 0,
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
        from aistock_agent.services.event_scraper import scrape_full_daily

        await scrape_full_daily(today)

    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_full_daily_skips_conduction_when_all_deduped():
    """I3 回归：全去重批次（合并后 persisted>0 但 added=0）→ 不触发传导。

    模拟 07:30 全量落库后，每小时增量批次全为已有 content_hash：
    旧守卫 persisted>0 会对整批重复触发传导（LLM 成本），added 守卫阻断。
    """
    today = shanghai_today().isoformat()
    major = _make_event(title="盘前重磅", impact_score=5)
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
        new=AsyncMock(return_value=[major]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 5,
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
        from aistock_agent.services.event_scraper import scrape_full_daily

        result = await scrape_full_daily(today)

    assert result["persisted"] == 5  # 合并后总数（对外契约不变）
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_full_daily_skips_conduction_when_no_major_events():
    """full_daily：无重大事件（全普通）→ 不触发传导。"""
    today = shanghai_today().isoformat()
    normal = _make_event(title="普通公告", impact_score=1)
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
        new=AsyncMock(return_value=[normal]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_global_markets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 1,
                "deduped": 0,
                "added": 1,
                "added_events": [normal],
                "error": None,
            }
        ),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        from aistock_agent.services.event_scraper import scrape_full_daily

        await scrape_full_daily(today)

    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_intraday_triggers_conduction_when_persisted():
    """intraday：有重大事件且本批有新增（added>0）→ 触发 _spawn_conduction。"""
    major = _make_event(title="盘中异动公告", impact_score=5)
    normal = _make_event(title="普通快讯", impact_score=1)
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[normal]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[major]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(
            return_value={
                "persisted": 1,
                "deduped": 0,
                "added": 1,
                "added_events": [major],
                "error": None,
            }
        ),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        from aistock_agent.services.event_scraper import scrape_intraday

        result = await scrape_intraday("2026-08-12")

    assert result["persisted"] == 1
    mock_spawn.assert_called_once()
    passed = mock_spawn.call_args.args[0]
    assert len(passed) == 1
    assert passed[0]["title"] == "盘中异动公告"


@pytest.mark.asyncio
async def test_scrape_intraday_skips_conduction_when_nothing_persisted():
    """intraday：落库失败/未新增（added=0）→ 不触发传导。"""
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
                "persisted": 0,
                "deduped": 1,
                "added": 0,
                "added_events": [],
                "error": "persist failed",
            }
        ),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        from aistock_agent.services.event_scraper import scrape_intraday

        await scrape_intraday("2026-08-12")

    mock_spawn.assert_not_called()
