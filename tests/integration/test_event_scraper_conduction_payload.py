"""event_scraper._trigger_conduction 透传 app_event_id 集成测试（spec §5A.3 P0）。

`_trigger_conduction` 内函数级 `from event_analysis_pipeline import
run_event_analysis_pipeline`（运行期取源模块属性），monkeypatch 必须设源模块
属性（from-import 绑定陷阱，对齐 event_scraper 模块 docstring 备注）；
`_mark_conduction_triggered` 必须 patch 防真实 Redis 写入。
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import event_scraper as scraper_mod
from aistock_agent.services.event_store import EventRecord


def _rec(app_event_id=None, app_event_status=None) -> EventRecord:
    return EventRecord(
        event_id="2026-09-15-abcdef1234567890",
        title="美联储议息",
        summary="9/17",
        url="http://x",
        impact_score=5,
        direction="unknown",
        involved_keywords=[],
        source="calendar",
        source_level="A",
        content_hash="abcdef1234567890",
        scrape_at="2026-09-15 08:00:00",
        score_date="2026-09-15",
        payload={},
        symbol="",
        stock_name="",
        industry="",
        event_scope="UNKNOWN",
        event_scope_source="rule",
        event_scope_confidence=0.0,
        app_event_id=app_event_id,
        app_event_status=app_event_status,
    )


@pytest.mark.asyncio
async def test_trigger_conduction_carries_app_event_id(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_pipeline(events):
        captured["events"] = events
        return {"status": "ok"}

    # _trigger_conduction 函数内 `from ... import run_event_analysis_pipeline`，
    # 故 patch 目标为 event_analysis_pipeline 模块属性（每次调用重新读取）。
    monkeypatch.setattr(
        "aistock_agent.services.event_analysis_pipeline.run_event_analysis_pipeline",
        fake_pipeline,
    )
    with patch.object(scraper_mod, "_mark_conduction_triggered", AsyncMock()):
        await scraper_mod._trigger_conduction([_rec(app_event_id="EVT-0001")])
    assert captured["events"][0]["app_event_id"] == "EVT-0001"


@pytest.mark.asyncio
async def test_trigger_conduction_omits_app_event_id_when_absent(monkeypatch):
    """缺省不落 app_event_id 键：major_events 形状与既有行为逐字节不变

    （向后兼容：既有 test_trigger_conduction_maps_major_event_fields 精确断言
    major_events dict 整体相等）。
    """
    captured: dict[str, object] = {}

    async def fake_pipeline(events):
        captured["events"] = events
        return {"status": "ok"}

    monkeypatch.setattr(
        "aistock_agent.services.event_analysis_pipeline.run_event_analysis_pipeline",
        fake_pipeline,
    )
    with patch.object(scraper_mod, "_mark_conduction_triggered", AsyncMock()):
        await scraper_mod._trigger_conduction([_rec()])
    assert "app_event_id" not in captured["events"][0]


@pytest.mark.asyncio
async def test_trigger_conduction_carries_app_event_status(monkeypatch):
    """app_event_status 条件透传（守卫依赖物化回填的 status）。"""
    captured: dict[str, object] = {}

    async def fake_pipeline(events):
        captured["events"] = events
        return {"status": "ok"}

    monkeypatch.setattr(
        "aistock_agent.services.event_analysis_pipeline.run_event_analysis_pipeline",
        fake_pipeline,
    )
    with patch.object(scraper_mod, "_mark_conduction_triggered", AsyncMock()):
        await scraper_mod._trigger_conduction(
            [_rec(app_event_id="EVT-0001", app_event_status="scheduled")]
        )
    assert captured["events"][0]["app_event_status"] == "scheduled"


@pytest.mark.asyncio
async def test_scrape_intraday_backfills_app_ids_before_conduction(monkeypatch):
    """开关开启时：物化返回 info → 原地回填 added_events → 传导收到 app_event_id（A1a）。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from aistock_agent.config import settings

    major = {
        "event_id": "2026-09-15-abcdef1234567890",
        "title": "华为将于 2026-09-23 发布新品",
        "summary": "",
        "url": "",
        "impact_score": 5,
        "direction": "unknown",
        "involved_keywords": [],
        "source": "calendar",
        "source_level": "A",
        "content_hash": "abcdef1234567890",
        "scrape_at": "2026-09-15 08:00:00",
        "score_date": "2026-09-15",
        "payload": {},
        "symbol": "",
        "stock_name": "",
        "industry": "",
        "event_scope": "UNKNOWN",
        "event_scope_source": "rule",
        "event_scope_confidence": 0.0,
        "app_event_id": None,
        "app_event_status": None,
    }
    monkeypatch.setattr(settings, "event_entity_enabled", True)
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
                "persisted": 1,
                "deduped": 0,
                "added": 1,
                "added_events": [major],
                "error": None,
            }
        ),
    ), patch(
        "aistock_agent.services.event_scrape_sources._materialize_event_entity",
        new=AsyncMock(return_value={"event_id": "EVT-0001", "event_status": "scheduled"}),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        from aistock_agent.services.event_scraper import scrape_intraday

        await scrape_intraday("2026-09-15")
    passed = mock_spawn.call_args.args[0]
    assert passed[0]["app_event_id"] == "EVT-0001"
    assert passed[0]["app_event_status"] == "scheduled"


def _major_event(app_event_id=None, app_event_status=None) -> dict[str, object]:
    """构造满足 is_major_event 的 EventRecord 形状 dict（内容复用既有 major 样例）。"""
    return {
        "event_id": "2026-09-15-abcdef1234567890",
        "title": "华为将于 2026-09-23 发布新品",
        "summary": "",
        "url": "",
        "impact_score": 5,
        "direction": "unknown",
        "involved_keywords": [],
        "source": "calendar",
        "source_level": "A",
        "content_hash": "abcdef1234567890",
        "scrape_at": "2026-09-15 08:00:00",
        "score_date": "2026-09-15",
        "payload": {},
        "symbol": "",
        "stock_name": "",
        "industry": "",
        "event_scope": "UNKNOWN",
        "event_scope_source": "rule",
        "event_scope_confidence": 0.0,
        "app_event_id": app_event_id,
        "app_event_status": app_event_status,
    }


@pytest.mark.asyncio
async def test_scrape_intraday_persists_app_event_id_into_event_store(monkeypatch):
    """缺陷A：物化先于落库，落库 content 的 events 声明权威 app_event_id。

    走**真实** save_event_scrape（仅 patch node_api 捕获入参，不做整函数替换），
    断言写进事件库(content)里的那条事件带 app_event_id/app_event_status，
    而不是只在内存 added_events 回填——否则「从事件库重放再传导」拿不到权威 id，
    时间线 occurred 准入会因两侧 id 不等静默丢弃该事件。修复前此断言必须失败。
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from aistock_agent.config import settings
    from aistock_agent.services.event_scraper import scrape_intraday

    monkeypatch.setattr(settings, "event_entity_enabled", True)
    # 模拟真实 node_api：在调用时刻就把 content 序列化为 JSON 快照（而非持有
    # dict 引用）。否则物化后原地回填会"穿透"引用让修复前误通过；只有物化
    # 先于落库，快照才含权威 id（这才是缺陷A的真实判定点）。
    captured_content: dict[str, object] = {}

    async def fake_save(**kwargs: object) -> dict[str, str]:
        captured_content["value"] = json.loads(json.dumps(kwargs["content"]))
        return {"id": "r1"}

    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[_major_event()]),
    ), patch(
        "aistock_agent.services.event_store.node_api",
    ) as mock_api, patch(
        "aistock_agent.services.event_scrape_sources._materialize_event_entity",
        new=AsyncMock(return_value={"event_id": "EVT-0001", "event_status": "occurred"}),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        # save_event_scrape 内部先读当日已有（空库 → 空列表）再合并落库
        mock_api.get_analysis_report_quiet = AsyncMock(return_value=None)
        mock_api.save_analysis_report = fake_save
        await scrape_intraday("2026-09-15")

    store_events = captured_content["value"]["events"]
    assert len(store_events) == 1
    assert store_events[0]["app_event_id"] == "EVT-0001"
    assert store_events[0]["app_event_status"] == "occurred"
    # 传导仍触发，且传导收到的就是同一批已回填对象（权威 id 进入 payload）
    mock_spawn.assert_called_once()
    passed = mock_spawn.call_args.args[0]
    assert passed[0]["app_event_id"] == "EVT-0001"


@pytest.mark.asyncio
async def test_scrape_full_daily_persists_app_event_id_into_event_store(monkeypatch):
    """缺陷A（full_daily 同构回归）：物化先于落库，落库 content 的 events 声明权威 app_event_id。

    与 `test_scrape_intraday_persists_app_event_id_into_event_store` 完全同构：
    走**真实** save_event_scrape（仅 patch node_api 捕获入参）、只 patch
    `_materialize_event_entity` 返回权威 id、并以 JSON 快照捕获 content（物化在
    落库前的判定点）。full_daily 分支曾可能因物化错位而漏写权威 id，
    此用例保证 full_daily 与 intraday 行为一致，避免该分支再次错位无人拦截。
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from aistock_agent.config import settings
    from aistock_agent.services.event_scraper import scrape_full_daily

    monkeypatch.setattr(settings, "event_entity_enabled", True)
    captured_content: dict[str, object] = {}

    async def fake_save(**kwargs: object) -> dict[str, str]:
        captured_content["value"] = json.loads(json.dumps(kwargs["content"]))
        return {"id": "r1"}

    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[_major_event()]),
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
        "aistock_agent.services.event_scraper.forward_event_sources.collect_l3_forward",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_store.node_api",
    ) as mock_api, patch(
        "aistock_agent.services.event_scrape_sources._materialize_event_entity",
        new=AsyncMock(return_value={"event_id": "EVT-0001", "event_status": "occurred"}),
    ), patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        mock_api.get_analysis_report_quiet = AsyncMock(return_value=None)
        mock_api.save_analysis_report = fake_save
        await scrape_full_daily("2026-09-15")

    store_events = captured_content["value"]["events"]
    assert len(store_events) == 1
    assert store_events[0]["app_event_id"] == "EVT-0001"
    assert store_events[0]["app_event_status"] == "occurred"
    # 传导仍触发，且传导收到的就是同一批已回填对象（权威 id 进入 payload）
    mock_spawn.assert_called_once()
    passed = mock_spawn.call_args.args[0]
    assert passed[0]["app_event_id"] == "EVT-0001"


@pytest.mark.asyncio
async def test_scrape_intraday_switch_off_skips_materialize_but_conduces(monkeypatch):
    """开关关闭：物化 helper 短路（不调用 _materialize_event_entity）、
    落库不含 app_event_id、传导仍触发（旧路径逐字节不变）。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from aistock_agent.config import settings
    from aistock_agent.services.event_scraper import scrape_intraday

    monkeypatch.setattr(settings, "event_entity_enabled", False)
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[_major_event()]),
    ), patch(
        "aistock_agent.services.event_store.node_api",
    ) as mock_api, patch(
        "aistock_agent.services.event_scrape_sources._materialize_event_entity",
        new=AsyncMock(return_value={"event_id": "EVT-0001", "event_status": "occurred"}),
    ) as mock_materialize, patch(
        "aistock_agent.services.event_scraper._spawn_conduction",
        new=MagicMock(),
    ) as mock_spawn:
        mock_api.get_analysis_report_quiet = AsyncMock(return_value=None)
        mock_api.save_analysis_report = AsyncMock(return_value={"id": "r1"})
        await scrape_intraday("2026-09-15")

    mock_materialize.assert_not_awaited()
    # 开关关闭：app 字段保持 None（未被物化回填）。注意键恒存在（normalize_event
    # 的 EventRecord 恒声明 app_event_id/app_event_status=None），因此断言值而非键。
    call_kwargs = mock_api.save_analysis_report.call_args.kwargs
    store_events = call_kwargs["content"]["events"]
    assert store_events[0].get("app_event_id") is None
    assert store_events[0].get("app_event_status") is None
    # 开关关闭不影响传导触发（旧路径逐字节不变）
    mock_spawn.assert_called_once()
