"""case_builder —— 历史切片生成与 T 窗口固化"""

from datetime import datetime, timedelta, timezone

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_builder import build_case, case_path, list_cases, load_case

TZ = timezone(timedelta(hours=8))


def _valid_snapshot() -> dict[str, object]:
    """schema-valid 的最小 MarketTraceSnapshot（I3：空 a_share + 空 sources，
    与确定性 discovery 重算一致：insufficient_data / 全 unmatched 诊断）。"""
    return {
        "snapshot_id": "trace-20260731-test",
        "trade_date": "2026-07-31",
        "captured_at": "2026-07-31T15:35:00+08:00",
        "a_share": {},
        "sources": {},
        "missing_fields": ["a_share.indexes"],
        "data_availability": {},
        "collection_status": {},
        "phenomenon_discovery": {
            "status": "insufficient_data",
            "primary": None,
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "incomplete",
                "attribution_inputs": "missing",
                "causal_evidence": "not_ready",
            },
            "diagnostics": [
                {"rule": r, "matched": False, "evidence_ids": []}
                for r in (
                    "broad_rally",
                    "broad_decline",
                    "style_divergence",
                    "sector_concentration",
                    "sentiment_extreme",
                )
            ],
        },
    }


def _telegraph_around(t: datetime) -> list[dict[str, object]]:
    return [
        {
            "time": (t - timedelta(minutes=30)).isoformat(),
            "title": "隔夜美股三大指数集体收涨",
            "content": "纳斯达克涨 2.5%",
            "url": "https://cls.cn/detail/1",
        },
        {
            "time": (t + timedelta(hours=1)).isoformat(),  # 后验泄漏样本
            "title": "午后 A 股券商集体拉升",
            "content": "尾盘成交额放大",
            "url": "https://cls.cn/detail/2",
        },
    ]


@pytest.mark.asyncio
async def test_build_case_filters_post_event_records(iterate_data_dir: object) -> None:
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    case = await build_case(
        adapter,
        event_title="隔夜美股暴涨，A股高开",
        event_time=t,
        telegraph_records=_telegraph_around(t),
        market_snapshot=_valid_snapshot(),
    )
    # T 窗口只含 T 及之前的数据，无后验泄漏
    assert all(
        datetime.fromisoformat(rec["time"]) <= t  # type: ignore[arg-type]
        for rec in case["window_before"]["cls_telegraph"]
    )
    assert len(case["window_before"]["cls_telegraph"]) == 1
    assert case["window_before"]["market_snapshot"]["trade_date"] == "2026-07-31"
    assert case["ground_truth_ref"] == f"gt_{case['case_id']}"
    assert (case_path(case["case_id"])).exists()


@pytest.mark.asyncio
async def test_build_case_rejects_invalid_market_snapshot(iterate_data_dir: object) -> None:
    """I3 回归：非 schema-valid 的 market_snapshot（旧 shorthand 形状）在生成期抛 ValueError。"""
    t = datetime(2026, 7, 31, 9, 30, tzinfo=TZ)
    adapter = get_adapter("review")
    with pytest.raises(ValueError, match="market_snapshot 不符合 MarketTraceSnapshot 契约"):
        await build_case(
            adapter,
            event_title="隔夜美股暴涨，A股高开",
            event_time=t,
            telegraph_records=_telegraph_around(t),
            market_snapshot={"trade_date": "2026-07-31", "indexes": {"sh": 1.2}},
        )


def test_case_roundtrip(iterate_data_dir: object) -> None:
    case_id = "case_20260731_us_market_surge"
    case = load_case(case_id)
    assert case["event_title"] == "隔夜美股暴涨，A股高开"
    assert "market_snapshot" in case["window_before"]


def test_list_cases(iterate_data_dir: object) -> None:
    ids = list_cases("review")
    assert any(cid == "case_20260731_us_market_surge" for cid in ids)
