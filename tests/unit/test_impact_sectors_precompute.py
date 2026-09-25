"""未来事件影响板块预计算 — 纯函数单元测试（2026-09-24）。

覆盖：
- _eligible_for_precompute：仅 calendar 未来事件且 impact_sectors 为空才处理
- _select_top_industries：按 similarity 降序 Top3、去重、保序
- _non_empty_sectors：非法/缺失/空数组视为空
"""

import pytest

from aistock_agent.services.impact_sectors_precompute import (
    _eligible_for_precompute,
    _non_empty_sectors,
    _select_top_industries,
)


def _row(**overrides) -> dict:
    base = {
        "event_id": "EVT-1",
        "title": "2026国际光伏产业大会",
        "source_type": "calendar",
        "event_status": "scheduled",
        "impact_sectors": [],
    }
    base.update(overrides)
    return base


class TestEligibleForPrecompute:
    def test_calendar_scheduled_empty_is_eligible(self):
        assert _eligible_for_precompute(_row()) is True

    def test_upcoming_is_eligible(self):
        assert _eligible_for_precompute(_row(event_status="upcoming")) is True

    def test_non_calendar_source_skipped(self):
        assert _eligible_for_precompute(_row(source_type="news")) is False

    def test_occurred_or_ongoing_skipped(self):
        assert _eligible_for_precompute(_row(event_status="occurred")) is False
        assert _eligible_for_precompute(_row(event_status="ongoing")) is False

    def test_already_has_sectors_skipped(self):
        assert _eligible_for_precompute(_row(impact_sectors=["光伏"])) is False

    def test_impact_sectors_missing_or_invalid_treated_empty(self):
        assert _eligible_for_precompute(_row(impact_sectors=None)) is True
        assert _eligible_for_precompute(_row(impact_sectors="not-list")) is True

    def test_non_dict_skipped(self):
        assert _eligible_for_precompute(None) is False  # type: ignore[arg-type]
        assert _eligible_for_precompute("x") is False  # type: ignore[arg-type]


class TestNonEmptySectors:
    def test_empty_and_invalid_are_false(self):
        assert _non_empty_sectors([]) is False
        assert _non_empty_sectors(None) is False
        assert _non_empty_sectors("[]") is False
        assert _non_empty_sectors([""]) is False
        assert _non_empty_sectors([1, 2]) is False

    def test_has_string_sector_true(self):
        assert _non_empty_sectors(["光模块"]) is True


class TestSelectTopIndustries:
    def test_sorted_by_similarity_desc_and_top_n(self):
        industries = [
            {"name": "PCB", "similarity": 0.72},
            {"name": "光模块", "similarity": 0.92},
            {"name": "半导体", "similarity": 0.85},
            {"name": "AI算力", "similarity": 0.8},
        ]
        assert _select_top_industries(industries, top_n=3) == ["光模块", "半导体", "AI算力"]

    def test_dedup_and_preserve_order(self):
        industries = [
            {"name": "光伏", "similarity": 0.9},
            {"name": "光伏", "similarity": 0.88},
            {"name": "储能", "similarity": 0.85},
        ]
        assert _select_top_industries(industries, top_n=3) == ["光伏", "储能"]

    def test_empty_industries_returns_empty(self):
        assert _select_top_industries(None) == []
        assert _select_top_industries([]) == []

    def test_invalid_entries_skipped(self):
        industries = [
            {"name": "", "similarity": 0.99},
            {"name": "   ", "similarity": 0.99},
            {"name": "锂矿", "similarity": 0.7},
            42,  # 非 dict 行跳过
        ]
        assert _select_top_industries(industries, top_n=3) == ["锂矿"]
