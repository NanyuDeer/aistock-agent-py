"""早报取数回退：last-close 不可用时必须回退到上一交易日收盘快照。"""

from __future__ import annotations

from datetime import date

import pytest

from aistock_agent.brain_v4.runtime import inputs as runtime_inputs
from aistock_agent.brain_v4.runtime.inputs import (
    _previous_weekday,
    build_briefing_chain,
)


class _CloseOnlyClient:
    """last-close 永远返回 None；指定日期的收盘快照可用。"""

    def __init__(self, available: dict[str, dict[str, object]]) -> None:
        self._available = available
        self.calls: list[str] = []

    async def get_close_snapshot(self, snapshot_date: str):
        self.calls.append(snapshot_date)
        return self._available.get(snapshot_date)

    async def get_last_close_snapshot(self):
        return None

    async def get_intraday_sectors(self):
        return None

    async def get_calendar_events(self, date_from: str, date_to: str, **kwargs):
        return []

    async def get_review_analysis_report(self, report_date: date):
        raise RuntimeError("legacy 不可用")


def test_previous_weekday_skips_weekend() -> None:
    assert _previous_weekday(date(2026, 11, 26)) == date(2026, 11, 25)
    # 2026-11-23 是周一，上一交易日应为周五 11-20。
    assert _previous_weekday(date(2026, 11, 23)) == date(2026, 11, 20)


@pytest.mark.asyncio
async def test_last_close_none_falls_back_to_previous_close() -> None:
    snapshot = {
        "status": "ok",
        "indexes": [{"ts_code": "000300.SH", "pct_chg": 0.5}],
        "breadth": {"advance_count": 3000, "decline_count": 2000},
    }
    client = _CloseOnlyClient({"20261125": snapshot})

    result = await build_briefing_chain(client, date(2026, 11, 26), "briefing_morning")

    assert "20261125" in client.calls
    assert "last_close_snapshot" not in result.missing_fields
    assert runtime_inputs.__name__  # 保持显式引用，避免未使用告警
