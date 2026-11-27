"""午报盘内快照的时间纪律：晚于 as_of 的“现在盘面”必须整段丢弃。

实测背景：`/internal/market/sectors` 的 `captured_at` 是请求时刻。回放历史日期
时若直接使用，午报会把“今天上午”的指数涨跌写成回放日的结果（与此前
`last_close` 当基线属同一类未来函数）。本文件锁定两条边界：
- captured_at 晚于 midday as_of → 不入证据、午报 summary 走降级文案；
- captured_at 落在时段内 → 正常进入证据与 summary。
"""

from __future__ import annotations

from datetime import date

import pytest

from aistock_agent.brain_v4.runtime.inputs import build_briefing_chain

CLOSE_SNAPSHOT: dict[str, object] = {
    "status": "complete",
    "trade_date": "20261126",
    "captured_at": "2026-11-26T15:10:00+08:00",
    "indexes": [
        {"ts_code": "000001.SH", "name": "上证指数", "close": 3476.88, "pct_chg": -0.51}
    ],
    "breadth": {"advance_count": 1766, "decline_count": 3642},
}


class _MiddayClient:
    def __init__(self, intraday: dict[str, object]) -> None:
        self._intraday = intraday

    async def get_close_snapshot(self, snapshot_date: str):
        return dict(CLOSE_SNAPSHOT) if snapshot_date == "20261126" else None

    async def get_last_close_snapshot(self):
        return dict(CLOSE_SNAPSHOT)

    async def get_intraday_sectors(self, *, include_breadth: bool = True):
        assert include_breadth is False  # V4 必须关闭全市场宽度
        return dict(self._intraday)

    async def get_calendar_events(self, date_from: str, date_to: str, **kwargs):
        return []

    async def get_review_analysis_report(self, report_date: date):
        raise RuntimeError("legacy 不可用")


@pytest.mark.asyncio
async def test_stale_intraday_is_discarded_for_replay(monkeypatch) -> None:
    from aistock_agent.brain_v4.runtime import inputs as runtime_inputs

    monkeypatch.setattr(runtime_inputs, "_archived_intraday", lambda day: None)
    captured_later = {
        "captured_at": "2026-11-27T18:15:00+08:00",
        "indexes": [{"code": "000001", "name": "上证指数", "close": 3606.49, "pct_chg": 3.73}],
        "gainers": [{"name": "半导体", "pct_change": 2.2}],
        "losers": [{"name": "煤炭", "pct_change": -1.1}],
        "availability": {"state": "available"},
    }
    client = _MiddayClient(captured_later)

    result = await build_briefing_chain(client, date(2026, 11, 26), "briefing_midday")

    source_ids = [seed.source_id for seed in result.chain.items]
    assert "SECTOR_INTRADAY" not in source_ids
    assert "不可用" in result.chain.midday.summary
    assert "3.73" not in result.chain.midday.summary
    assert "intraday_sectors" in result.missing_fields


@pytest.mark.asyncio
async def test_in_window_intraday_enters_midday_evidence(monkeypatch) -> None:
    from aistock_agent.brain_v4.runtime import inputs as runtime_inputs

    monkeypatch.setattr(runtime_inputs, "_archived_intraday", lambda day: None)
    captured_in_window = {
        "captured_at": "2026-11-26T11:45:00+08:00",
        "indexes": [{"code": "000001", "name": "上证指数", "close": 3490.1, "pct_chg": 1.11}],
        "gainers": [{"name": "半导体", "pct_change": 2.2}],
        "losers": [{"name": "煤炭", "pct_change": -1.1}],
        "availability": {"state": "available"},
    }
    client = _MiddayClient(captured_in_window)

    result = await build_briefing_chain(client, date(2026, 11, 26), "briefing_midday")

    source_ids = [seed.source_id for seed in result.chain.items]
    assert "SECTOR_INTRADAY" in source_ids
    assert "上证指数" in result.chain.midday.summary
    assert "+1.11%" in result.chain.midday.summary
    assert "intraday_sectors" not in result.missing_fields


@pytest.mark.asyncio
async def test_archived_intraday_preferred_over_live(monkeypatch) -> None:
    """有当日归档时优先用归档，不回退到“现在”的实时盘面。"""
    from aistock_agent.brain_v4.runtime import inputs as runtime_inputs

    archived = {
        "captured_at": "2026-11-26T12:25:31+08:00",
        "indexes": [{"code": "000001", "name": "上证指数", "close": 3475.46, "pct_chg": 0.88}],
        "gainers": [{"name": "半导体", "pct_change": 2.2}],
        "losers": [{"name": "煤炭", "pct_change": -1.1}],
        "availability": {"state": "available"},
    }
    monkeypatch.setattr(runtime_inputs, "_archived_intraday", lambda day: dict(archived))
    stale_live = {
        "captured_at": "2026-11-27T18:15:00+08:00",
        "indexes": [{"code": "000001", "name": "上证指数", "close": 3606.49, "pct_chg": 3.73}],
        "gainers": [],
        "losers": [],
    }
    client = _MiddayClient(stale_live)

    result = await build_briefing_chain(client, date(2026, 11, 26), "briefing_midday")

    assert "SECTOR_INTRADAY" in [seed.source_id for seed in result.chain.items]
    # 用归档里的 0.88%，不能出现实时快照的 3.73%。
    assert "+0.88%" in result.chain.midday.summary
    assert "3.73" not in result.chain.midday.summary
