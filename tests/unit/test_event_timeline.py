from aistock_agent.services.event_timeline import (
    SOURCE_TYPE_TO_EVENT_TYPE,
    grade_importance,
    shanghai_start_date,
    to_timeline_event,
)


def test_shanghai_start_date_date_only():
    assert shanghai_start_date("2026-10-06") == "2026-10-06"


def test_shanghai_start_date_converts_utc_to_shanghai_day():
    # 2026-10-05T17:00:00Z = 上海 2026-10-06 01:00 → 归属 10-06
    assert shanghai_start_date("2026-10-05T17:00:00+00:00") == "2026-10-06"


def test_shanghai_start_date_invalid_returns_none():
    assert shanghai_start_date("not-a-date") is None
    assert shanghai_start_date(None) is None
    assert shanghai_start_date(123) is None


def test_grade_importance_manual_is_high():
    assert grade_importance({"title": "x", "source_type": "manual"}) == "high"


def test_grade_importance_macro_term_is_high():
    assert grade_importance({"title": "9月CPI发布", "source_type": "news"}) == "high"


def test_grade_importance_news_is_medium():
    assert grade_importance({"title": "某公司公告", "source_type": "news"}) == "medium"


def test_grade_importance_unknown_is_low():
    assert grade_importance({"title": "x", "source_type": "weird"}) == "low"


def test_to_timeline_event_maps_fields():
    entity = {
        "title": "9月CPI发布",
        "event_start_time": "2026-10-09T01:30:00+08:00",
        "source_type": "calendar",
    }
    assert to_timeline_event(entity) == {
        "date": "2026-10-09",
        "type": SOURCE_TYPE_TO_EVENT_TYPE["calendar"],
        "title": "9月CPI发布",
        "importance": "high",
    }


def test_to_timeline_event_skips_bad_date_or_empty_title():
    assert to_timeline_event({"title": "x", "event_start_time": "bad"}) is None
    assert to_timeline_event({"title": "  ", "event_start_time": "2026-10-09"}) is None
