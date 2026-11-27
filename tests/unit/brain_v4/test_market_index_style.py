"""P11：indexes 列表解析与大小盘风格派生（全部离线）。

缺陷（服务器探针已确证）：
- close/last_close 快照的 `indexes` 实际是 **list**（6 个元素），旧代码用
  `_as_mapping` 把 list 判成 None → `audit.missing_fields` 长期含 `indexes`；
- 快照没有 `style` 键，旧代码只做 `close.get("style")` → `style` 永远是 missing。

修复：
- `_normalize_indexes` 兼容 list/dict，用稳定键（优先 `code`，其次 `symbol`，
  再退化为 `name`）归一化成 `{code: info}`；
- `_derive_style` 用沪深300/上证50 与中证1000/中证2000 的真实涨跌幅确定性
  推导大小盘风格，标注 `source="derived:indexes"`，绝不编造数值。

不触网、不读系统时间。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aistock_agent.brain_v4.runtime.inputs import (
    _derive_style,
    _normalize_indexes,
    build_market_trace_case,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
DATA_CUTOFF = datetime(2026, 11, 27, 15, 0, tzinfo=CST)


class _ReviewResult:
    def __init__(self, status: str, report: dict[str, object] | None) -> None:
        self.status = status
        self.report = report


class _ListIndexesNodeClient:
    """当日收盘快照可用、indexes 为 list 形态、且没有 style 键的假客户端。"""

    def __init__(self, close: dict[str, object]) -> None:
        self._close = close
        self.calls: list[str] = []

    async def get_close_snapshot(self, snapshot_date: str) -> dict[str, object]:
        self.calls.append("get_close_snapshot")
        return {key: value for key, value in self._close.items()}

    async def get_last_close_snapshot(self) -> dict[str, object] | None:
        self.calls.append("get_last_close_snapshot")
        return None

    async def get_intraday_sectors(
        self, *, include_breadth: bool = True
    ) -> dict[str, object] | None:
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


LIST_INDEXES_CLOSE: dict[str, object] = {
    "status": "complete",
    "trade_date": "20261126",
    "captured_at": "2026-11-26T15:10:00+08:00",
    "indexes": [
        {"code": "000300.SH", "name": "沪深300", "close": 3900.1, "change_pct": 0.85},
        {"code": "000016.SH", "name": "上证50", "close": 2600.2, "change_pct": 0.60},
        {"code": "000852.SH", "name": "中证1000", "close": 5900.3, "change_pct": -0.32},
        {"code": "932000.CSI", "name": "中证2000", "close": 2100.4, "change_pct": -0.50},
        {"code": "000001.SH", "name": "上证指数", "close": 3046.1, "change_pct": -0.10},
        {"code": "399001.SZ", "name": "深证成指", "close": 9800.5, "change_pct": 0.20},
    ],
    "breadth": {
        "total_count": 5200,
        "advance_count": 2400,
        "decline_count": 2600,
        "flat_count": 200,
    },
    "turnover": {"amount_yuan": 780000000000, "change_pct": -0.08},
    "sectors": {
        "top_gainers": [{"name": "银行", "change_pct": 1.2}],
        "top_losers": [{"name": "成长", "change_pct": -1.6}],
        "top_inflows": [],
        "top_outflows": [],
    },
}


def test_normalize_indexes_list_to_dict_keyed_by_code() -> None:
    normalized = _normalize_indexes(LIST_INDEXES_CLOSE["indexes"])

    assert isinstance(normalized, dict)
    assert len(normalized) == 6
    assert "000300.SH" in normalized
    assert normalized["000300.SH"]["name"] == "沪深300"


def test_normalize_indexes_dict_still_supported() -> None:
    raw = {"000300.SH": {"name": "沪深300", "change_pct": 0.85}}

    assert _normalize_indexes(raw) == raw


def test_normalize_indexes_skips_non_mapping_and_empty() -> None:
    # 非 mapping 元素跳过；无稳定键的元素跳过；全空返回 None。
    assert _normalize_indexes([1, "x", None]) is None
    assert _normalize_indexes([{"name": ""}]) is None
    normalized = _normalize_indexes([{"symbol": "000300.SH", "change": 0.85}])
    assert normalized is not None
    assert "000300.SH" in normalized


def test_derive_style_large_up_small_down() -> None:
    indexes = _normalize_indexes(LIST_INDEXES_CLOSE["indexes"])
    assert indexes is not None

    style = _derive_style(indexes)

    assert style is not None
    assert style["label"] == "大盘占优"
    assert style["spread"] == pytest.approx(1.17)
    assert style["source"] == "derived:indexes"
    assert style["method"] == "index_spread_v1"
    assert str(style["large_cap_code"]).startswith("000300")
    assert str(style["small_cap_code"]).startswith("000852")


def test_derive_style_requires_both_sides() -> None:
    # 只有一个基准 → 不得用单指数猜，返回 None。
    only_large = {"000300.SH": {"change_pct": 0.85}}
    assert _derive_style(only_large) is None


@pytest.mark.asyncio
async def test_market_trace_list_indexes_fills_missing_and_derives_style() -> None:
    client = _ListIndexesNodeClient(LIST_INDEXES_CLOSE)

    inputs = await build_market_trace_case(
        client, AS_OF, DATA_CUTOFF, "snap_p11_index_style"
    )

    assert "indexes" not in inputs.missing_fields
    assert "style" not in inputs.missing_fields

    snapshot = inputs.case.snapshot
    assert "indexes" not in snapshot.missing_fields
    assert "style" not in snapshot.missing_fields

    # indexes 被归一化成 dict（键=指数代码）供下游使用。
    assert isinstance(snapshot.a_share.get("indexes"), dict)
    assert "000300.SH" in snapshot.a_share["indexes"]

    # 派生风格写入 a_share 与来源；标注 derived，不伪装成上游原始字段。
    style = snapshot.a_share.get("style")
    assert isinstance(style, dict)
    assert style["label"] == "大盘占优"
    assert "STYLE_ALL" in snapshot.sources
    assert snapshot.sources["STYLE_ALL"].provider == "derived:indexes"

# 真实快照形态（tushare:index_daily）：`ts_code` + `pct_chg`。
# 之前只认 code/symbol/name 与 change_pct/change/pct_change，导致 style 永远缺失。
REAL_INDEXES = [
    {
        "ts_code": "000300.SH",
        "name": "沪深300",
        "pct_chg": 0.8500,
        "source": "tushare:index_daily",
    },
    {
        "ts_code": "000852.SH",
        "name": "中证1000",
        "pct_chg": -0.6200,
        "source": "tushare:index_daily",
    },
]


@pytest.mark.asyncio
async def test_real_ts_code_and_pct_chg_shape_derives_style() -> None:
    client = _ListIndexesNodeClient({**LIST_INDEXES_CLOSE, "indexes": REAL_INDEXES})

    inputs = await build_market_trace_case(
        client, AS_OF, DATA_CUTOFF, "snap_p11_real_shape"
    )

    assert "style" not in inputs.missing_fields
    style = inputs.case.snapshot.a_share.get("style")
    assert isinstance(style, dict)
    assert style["label"] == "大盘占优"
    assert style["large_cap_code"] == "000300.SH"
    assert style["small_cap_code"] == "000852.SH"

