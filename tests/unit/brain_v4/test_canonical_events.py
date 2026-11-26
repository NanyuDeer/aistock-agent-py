"""Canonical event 适配测试：多来源聚合、更正版本与初始触发时间。

覆盖（Task 3 Step 4）：
- agent event_store、app-api event_entities、calendar 三来源同一事件 → 一个
  event_id、三条 observation；
- 更正记录产生更高的 lifecycle_version，canonical 状态为 corrected；
- published_at 晚于价格起点的观察不得标为初始触发。
"""

from datetime import datetime

from aistock_agent.brain_v4.events.adapters import (
    adapt_agent_event,
    adapt_calendar_row,
    adapt_event_entity,
    can_mark_initial_trigger,
    resolve_canonical_events,
)

AS_OF = datetime.fromisoformat("2026-11-26T15:00:00+08:00")
FETCHED_AT = datetime.fromisoformat("2026-11-26T15:00:01+08:00")
TITLE = "某某公司发布三季度业绩预告"


def _agent_row(
    *,
    source_id: str = "src-news-1",
    title: str = TITLE,
    scrape_at: str = "2026-11-20T10:05:00+08:00",
    score_date: str = "2026-11-20",
) -> dict[str, object]:
    return {
        "event_id": source_id,
        "title": title,
        "summary": "业绩预告披露",
        "url": "https://example.com/news/1",
        "source": "财联社",
        "score_date": score_date,
        "scrape_at": scrape_at,
        "app_event_id": "EVT-1",
    }


def _entity_row() -> dict[str, object]:
    return {
        "event_id": "EVT-1",
        "title": TITLE,
        "summary": None,
        "source_type": "news",
        "source_event_id": "src-news-1",
        "publish_time": "2026-11-20T10:06:00+08:00",
        "event_start_time": "2026-11-20T10:00:00+08:00",
        "time_confidence": 0.9,
        "time_source": "calendar",
    }


def _calendar_row() -> dict[str, object]:
    return {
        "event_date": "2026-11-20",
        "event_time": "10:00",
        "title": TITLE,
        "detail": "业绩预告披露",
        "importance": "high",
        "source": "L4",
    }


def test_three_sources_resolve_to_one_event_with_three_observations() -> None:
    agent_obs = adapt_agent_event(_agent_row(), as_of=AS_OF, fetched_at=FETCHED_AT)
    entity_obs = adapt_event_entity(_entity_row(), as_of=AS_OF, fetched_at=FETCHED_AT)
    calendar_obs = adapt_calendar_row(_calendar_row(), as_of=AS_OF, fetched_at=FETCHED_AT)

    records = resolve_canonical_events([agent_obs, entity_obs, calendar_obs])

    assert len(records) == 1
    record = records[0]
    assert record.event_id == agent_obs.event_id == entity_obs.event_id == calendar_obs.event_id
    assert len(record.observation_ids) == 3
    assert len(set(record.source_refs)) == 3
    assert record.lifecycle_version == 1
    assert record.dedupe_fingerprint


def test_correction_increments_lifecycle_version() -> None:
    original = adapt_agent_event(_agent_row(), as_of=AS_OF, fetched_at=FETCHED_AT)
    corrected = adapt_agent_event(
        _agent_row(
            source_id="src-news-1-correction",
            scrape_at="2026-11-20T12:30:00+08:00",
        ),
        as_of=AS_OF,
        fetched_at=FETCHED_AT,
        lifecycle_version=2,
    )

    records = resolve_canonical_events([original, corrected])

    assert len(records) == 1
    record = records[0]
    assert record.lifecycle_version == 2
    assert record.status == "corrected"
    assert len(record.observation_ids) == 2


def test_published_after_price_start_cannot_be_initial_trigger() -> None:
    price_start = datetime.fromisoformat("2026-11-20T09:30:00+08:00")
    late = adapt_agent_event(
        _agent_row(scrape_at="2026-11-20T10:05:00+08:00"),
        as_of=AS_OF,
        fetched_at=FETCHED_AT,
    )
    early = adapt_agent_event(
        _agent_row(scrape_at="2026-11-20T09:10:00+08:00"),
        as_of=AS_OF,
        fetched_at=FETCHED_AT,
    )

    assert can_mark_initial_trigger(late, price_start) is False
    assert can_mark_initial_trigger(early, price_start) is True


def test_conflicting_titles_are_recorded_not_overwritten() -> None:
    a = adapt_agent_event(_agent_row(), as_of=AS_OF, fetched_at=FETCHED_AT)
    # 标题仅在书写上不同（全角/标点），归一化后仍是同一事件，不应算冲突。
    same = adapt_agent_event(
        _agent_row(source_id="src-news-2", title="某某公司发布三季度业绩预告。"),
        as_of=AS_OF,
        fetched_at=FETCHED_AT,
    )
    records = resolve_canonical_events([a, same])
    assert len(records) == 1
    assert records[0].source_conflicts == []
