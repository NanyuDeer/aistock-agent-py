"""P9：收盘快照缺失时回退 `last_close_snapshot`（全部离线）。

缺陷：`close-snapshot` 返回 409（当日快照未生成）时，`build_market_trace_case`
只把 indexes/breadth/sectors 记进 `missing_fields`，不会回退到
`get_last_close_snapshot()`；而探针显示 `last_close` 能返回这些字段。

本文件用假 `NodeApiClient` 验证：
- 当日快照抛异常/返回 None、last_close 有数据 → indexes/breadth/sectors 不再
  出现在 missing_fields，且标注 `data_source="last_close"`；
- 两者都缺 → 仍如实标记 missing，不编造数值。

不触网、不读系统时间。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aistock_agent.brain_v4.runtime.inputs import build_market_trace_case

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
DATA_CUTOFF = datetime(2026, 11, 27, 15, 0, tzinfo=CST)

LAST_CLOSE_SNAPSHOT: dict[str, object] = {
    "status": "complete",
    "trade_date": "20261126",
    "captured_at": "2026-11-26T15:10:00+08:00",
    "indexes": {
        "000001.SH": {
            "ts_code": "000001.SH",
            "name": "上证指数",
            "close": 3046.1,
            "change_pct": -1.02,
        }
    },
    "breadth": {
        "total_count": 5200,
        "advance_count": 2050,
        "decline_count": 2980,
        "flat_count": 170,
    },
    "turnover": {"amount_yuan": 780000000000, "change_pct": -0.08},
    "style": {"large_cap_change_pct": -0.7, "small_cap_change_pct": -1.5},
    "sectors": {
        "defensive": {"change_pct": 0.6},
        "growth": {"change_pct": -1.6},
    },
}


class _ReviewResult:
    def __init__(self, status: str, report: dict[str, object] | None) -> None:
        self.status = status
        self.report = report


class _FallbackNodeClient:
    """当日快照不可用、last_close 可用的假客户端。"""

    def __init__(
        self,
        *,
        close_mode: str,
        last_close: dict[str, object] | None,
    ) -> None:
        self._close_mode = close_mode
        self._last_close = last_close
        self.calls: list[str] = []

    async def get_close_snapshot(
        self, snapshot_date: str
    ) -> dict[str, object] | None:
        self.calls.append("get_close_snapshot")
        if self._close_mode == "raise":
            raise RuntimeError("close-snapshot 409")
        return None

    async def get_last_close_snapshot(self) -> dict[str, object] | None:
        self.calls.append("get_last_close_snapshot")
        return None if self._last_close is None else dict(self._last_close)

    async def get_intraday_sectors(self) -> dict[str, object] | None:
        self.calls.append("get_intraday_sectors")
        return None

    async def get_calendar_events(
        self,
        date_from: str,
        date_to: str,
        *,
        importance: str | None = None,
    ) -> list[dict[str, object]] | None:
        self.calls.append("get_calendar_events")
        return []

    async def get_review_analysis_report(self, report_date: date) -> _ReviewResult:
        return _ReviewResult("not_found", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["none", "raise"])
async def test_missing_close_snapshot_falls_back_to_last_close(close_mode: str) -> None:
    client = _FallbackNodeClient(
        close_mode=close_mode, last_close=LAST_CLOSE_SNAPSHOT
    )

    inputs = await build_market_trace_case(
        client, AS_OF, DATA_CUTOFF, "snap_p9_last_close"
    )

    # 关键字段不再丢：indexes/breadth/sectors 已由 last_close 回填。
    for field in ("indexes", "breadth", "sectors", "style", "turnover"):
        assert field not in inputs.missing_fields, f"{field} 不应再缺失"

    # 明确标注数据来源为 last_close（审计可按此字段区分当日/上一交易日）。
    assert inputs.data_source == "last_close"

    snapshot = inputs.case.snapshot
    assert "indexes" in snapshot.a_share
    assert "breadth" in snapshot.a_share
    assert "sectors" in snapshot.a_share

    # 来源记录必须透出 last-close 提供方，避免 payload 把上一交易日伪装成当日。
    index_sources = [
        source
        for source_id, source in snapshot.sources.items()
        if source_id.startswith("INDEX_")
    ]
    assert index_sources
    assert all(
        source.provider == "node_api:last-close-snapshot"
        for source in index_sources
    )
    assert "get_last_close_snapshot" in client.calls


@pytest.mark.asyncio
async def test_both_snapshots_missing_still_marks_fields_missing() -> None:
    client = _FallbackNodeClient(close_mode="none", last_close=None)

    inputs = await build_market_trace_case(
        client, AS_OF, DATA_CUTOFF, "snap_p9_no_close"
    )

    # 两者都缺：如实标记 missing，绝不编造数值。
    for field in ("indexes", "breadth", "sectors"):
        assert field in inputs.missing_fields
    assert inputs.data_source == "unavailable"
    assert inputs.case.snapshot.a_share == {}
