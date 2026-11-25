"""EventRecord `app_event_id` 可选键测试（spec §4.2 source_event_id 语义）。

EventRecord 为 TypedDict（运行时无校验，纯类型契约）；本文件同时覆盖
normalize_event / load_event_scrape 两个构造点的缺省值兜底（均为真 RED：
实现前 `"app_event_id" in ev` 为 False / 读回键 KeyError）。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_store import (
    EventRecord,
    load_event_scrape,
    normalize_event,
)


def test_event_record_carries_optional_app_event_id() -> None:
    # 新键可空、缺省构造不报错（向后兼容：历史数据可能缺键，一律 .get 消费）
    ev: EventRecord = {
        "event_id": "2026-09-15-abcdef1234567890",
        "title": "美联储议息",
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
        "app_event_id": "EVT-0001",
    }
    assert ev["app_event_id"] == "EVT-0001"
    # 缺省消费 .get 返回 None（历史数据无该键）
    assert ev.get("app_event_id") == "EVT-0001"
    ev2 = {k: v for k, v in ev.items() if k != "app_event_id"}
    assert ev2.get("app_event_id") is None


def test_normalize_event_exposes_app_event_id_default_none() -> None:
    """normalize_event 不产出 app_event_id（来源侧无权威 id），但键存在且为 None。"""
    ev = normalize_event(
        {"title": "美联储议息", "url": "http://x"}, source="calendar", score_date="2026-09-15"
    )
    assert ev is not None
    assert "app_event_id" in ev
    assert ev["app_event_id"] is None


@pytest.mark.asyncio
async def test_load_event_scrape_app_event_id_roundtrip() -> None:
    """load_event_scrape 读回 app_event_id：存储有值保留；无键（历史数据）默认 None。"""
    stored = {
        "content": {
            "events": [
                {
                    "event_id": "e1",
                    "title": "A",
                    "impact_score": 5,
                    "app_event_id": "EVT-0001",
                },
                {"event_id": "e2", "title": "B", "impact_score": 5},
            ],
            "schema_version": "1.0",
        }
    }
    with patch(
        "aistock_agent.services.event_store.node_api.get_analysis_report_quiet",
        new=AsyncMock(return_value=stored),
    ):
        events = await load_event_scrape("2026-09-15")
    assert events[0]["app_event_id"] == "EVT-0001"
    # 历史数据无 app_event_id 键 → .get 返回 None（兼容缺键）
    assert events[1].get("app_event_id") is None
