"""S5 传导门控：peripheral（外围资讯）不进 event_agent/GI（Task 9）。

`_trigger_conduction` 对 peripheral 事件记 EVENT_CONDUCTION_FILTER → 跳过传导；
事件仍保留在事件库（本任务不动写路径）；major_events 键集合不变。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import event_scraper as scr

_PIPELINE_PATH = "aistock_agent.services.event_analysis_pipeline.run_event_analysis_pipeline"


def _ev(event_id, title, *, event_scope="MARKET", event_origin="peripheral", source="cls"):
    return {
        "event_id": event_id, "title": title, "summary": "", "url": "",
        "impact_score": 5, "direction": "neutral", "involved_keywords": [],
        "source": source, "event_scope": event_scope, "event_origin": event_origin,
        "score_date": "2026-10-15",
    }


def _capture(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_pipeline(events):
        captured["events"] = events
        return {"error": None}

    monkeypatch.setattr(_PIPELINE_PATH, fake_pipeline)
    return captured


@pytest.mark.asyncio
async def test_peripheral_event_skipped_but_kept(monkeypatch):
    captured = _capture(monkeypatch)
    with patch.object(scr, "_mark_conduction_triggered", AsyncMock()):
        await scr._trigger_conduction([_ev("x", "恒生指数 隔夜表现")])
    assert "events" not in captured                    # 未触发传导


@pytest.mark.asyncio
async def test_non_peripheral_event_still_conducts(monkeypatch):
    captured = _capture(monkeypatch)
    with patch.object(scr, "_mark_conduction_triggered", AsyncMock()):
        await scr._trigger_conduction([_ev("y", "大盘午后跳水", event_origin="domestic")])
    assert captured["events"][0]["event_id"] == "y"


@pytest.mark.asyncio
async def test_mixed_batch_skips_only_peripheral(monkeypatch):
    captured = _capture(monkeypatch)
    with patch.object(scr, "_mark_conduction_triggered", AsyncMock()):
        await scr._trigger_conduction([
            _ev("p", "恒生指数 隔夜表现", event_origin="peripheral"),
            _ev("d", "大盘午后跳水", event_origin="domestic"),
        ])
    assert [e["event_id"] for e in captured["events"]] == ["d"]
