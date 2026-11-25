"""event_persister 回写 appEventId 测试（spec §4.3）。

真实调用形态：persist_event_report(event_id, event_meta, event_text,
analysis_reports) → node_api.post("/internal/analysis-reports", {...})；
eventId 保留现状（隔离键语义不变），appEventId 仅加性回写。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_persister import persist_event_report


def _posted_body(api) -> dict[str, object]:
    call = api.post.call_args
    if call.kwargs.get("body"):
        return call.kwargs["body"]
    # 位置调用形态：post(path, body)
    return call.args[1]


@pytest.mark.asyncio
async def test_persist_content_includes_app_event_id():
    with patch("aistock_agent.services.event_persister.node_api") as api:
        api.post = AsyncMock(return_value={"id": 1})
        await persist_event_report(
            event_id="EVT-0001",
            event_meta={"title": "美联储议息", "source": "calendar"},
            event_text="请分析以下重大事件：美联储议息",
            analysis_reports={},
        )
    content = _posted_body(api)["content"]
    assert content["appEventId"] == "EVT-0001"


@pytest.mark.asyncio
async def test_persist_content_keeps_event_id_unchanged():
    """eventId 保留现状（隔离键语义不变），appEventId 仅加性回写。"""
    with patch("aistock_agent.services.event_persister.node_api") as api:
        api.post = AsyncMock(return_value={"id": 1})
        await persist_event_report(
            event_id="EVT-0001",
            event_meta={"title": "美联储议息"},
            event_text="text",
            analysis_reports={},
        )
    content = _posted_body(api)["content"]
    assert content["eventId"] == "EVT-0001"
    assert content["appEventId"] == "EVT-0001"


@pytest.mark.asyncio
async def test_persist_content_includes_app_event_status():
    """event_meta.event_status → content.appEventStatus 加性回写（spec §6.2 关联）。"""
    with patch("aistock_agent.services.event_persister.node_api") as api:
        api.post = AsyncMock(return_value={"id": 1})
        await persist_event_report(
            event_id="EVT-0001",
            event_meta={"title": "美联储议息", "event_status": "scheduled"},
            event_text="text",
            analysis_reports={},
        )
    content = _posted_body(api)["content"]
    assert content["eventId"] == "EVT-0001"
    assert content["appEventId"] == "EVT-0001"
    assert content["appEventStatus"] == "scheduled"
