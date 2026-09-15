"""event_scraper._trigger_conduction 透传 app_event_id 集成测试（spec §5A.3 P0）。

`_trigger_conduction` 内函数级 `from event_analysis_pipeline import
run_event_analysis_pipeline`（运行期取源模块属性），monkeypatch 必须设源模块
属性（from-import 绑定陷阱，对齐 event_scraper 模块 docstring 备注）；
`_mark_conduction_triggered` 必须 patch 防真实 Redis 写入。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import event_scraper as scraper_mod
from aistock_agent.services.event_store import EventRecord


def _rec(app_event_id=None) -> EventRecord:
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
