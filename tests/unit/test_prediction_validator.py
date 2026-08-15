from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import prediction_validator
from aistock_agent.services.prediction_validator import _INDEX_CODE_MAP, run_once


def _pending_record(record_id=1, due="2026-08-10", target="上证指数", direction="bullish"):
    return {
        "id": record_id,
        "source_type": "market_trace",
        "source_id": "review:2026-08-01",
        "prediction": {
            "horizons": [
                {
                    "horizon": "mid",
                    "target": target,
                    "direction": direction,
                    "metric_projection": "x",
                }
            ]
        },
        "due_dates": {"mid": due},
        "verification": {},
    }


def test_index_code_map_contains_common_indexes():
    assert _INDEX_CODE_MAP["上证指数"] == "000001"
    assert _INDEX_CODE_MAP["沪深300"] == "000300"


@pytest.mark.asyncio
async def test_run_once_verifies_due_horizon():
    record = _pending_record(due="2026-08-10")
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=[
                {"trade_date": "2026-08-10", "pct_chg": 1.2},  # due 当日 +1.2% → hit, strong_hit
                {"trade_date": "2026-08-11", "pct_chg": 0.3},
                {"trade_date": "2026-08-12", "pct_chg": -0.2},
                {"trade_date": "2026-08-13", "pct_chg": 0.1},
            ]),
        ),
        patch.object(
            prediction_validator.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 10),
        ),
    ):
        updated = await run_once()
    assert updated == 1
    entry = update.await_args.args[2]
    assert entry["result"] == "hit"
    assert entry["grade"] == "strong_hit"  # due 当日命中
    assert entry["actual"] == "+1.40%"


@pytest.mark.asyncio
async def test_run_once_approximate_horizon_reason_prefix():
    """近似档（prediction.due_dates_approximate 含该档）→ 验证 reason 带 (approximate_due_date)
    前缀，供统计分桶归因（P2 裁决：越年档到期日为近似，需与精确档区分）。"""
    record = _pending_record(due="2026-08-10")
    record["prediction"]["due_dates_approximate"] = ["mid"]
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=[
                {"trade_date": "2026-08-10", "pct_chg": -0.8},  # bullish 当日 -0.8% → miss
                {"trade_date": "2026-08-11", "pct_chg": -0.5},
                {"trade_date": "2026-08-12", "pct_chg": -0.3},
                {"trade_date": "2026-08-13", "pct_chg": -0.1},
            ]),
        ),
        patch.object(
            prediction_validator.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 10),
        ),
    ):
        updated = await run_once()
    assert updated == 1
    entry = update.await_args.args[2]
    assert entry["result"] == "miss"  # bullish 但窗口内无 >0 日 → miss
    assert "(approximate_due_date)" in entry["reason"]


@pytest.mark.asyncio
async def test_run_once_skips_not_due_and_unknown_target():
    record = _pending_record(due="2026-09-01", target="半导体板块")
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 10),
        ),
    ):
        updated = await run_once()
    assert updated == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_verify_bullish_window_hit_with_grade():
    """v2：bullish 档窗口内任一日 >0 → hit；due 当日命中 → grade=strong_hit。"""
    record = _pending_record(due="2026-08-10", direction="bullish")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": -0.5},  # due 当日（未命中）
        {"trade_date": "2026-08-11", "pct_chg": 1.8},   # 窗口内命中
        {"trade_date": "2026-08-12", "pct_chg": 0.2},
        {"trade_date": "2026-08-13", "pct_chg": -0.1},
    ]
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions", new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "get_index_kline", new=AsyncMock(return_value=kline_rows)),
        patch.object(prediction_validator.node_api, "update_prediction_verification", new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today", return_value=date(2026, 8, 13)),
    ):
        updated = await run_once()
    assert updated == 1
    entry = update.await_args.args[2]
    assert entry["result"] == "hit"
    assert entry["grade"] == "hit"        # 非 due 当日命中、窗口无 >=5% → 普通 hit
    assert entry["methodology_version"] == "2.0"
    assert "baseline_neutral" in entry


@pytest.mark.asyncio
async def test_v2_bullish_no_sign_hit_is_miss_without_fallback():
    """v2 核心：bullish 窗口内无 >0 日 → miss，且无累计净值兜底（不因累计为正而翻成 hit）。"""
    record = _pending_record(due="2026-08-10", direction="bullish")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": -1.0},
        {"trade_date": "2026-08-11", "pct_chg": -2.0},
        {"trade_date": "2026-08-12", "pct_chg": -0.5},
        {"trade_date": "2026-08-13", "pct_chg": -0.3},
    ]
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions", new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "get_index_kline", new=AsyncMock(return_value=kline_rows)),
        patch.object(prediction_validator.node_api, "update_prediction_verification", new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today", return_value=date(2026, 8, 13)),
    ):
        updated = await run_once()
    assert updated == 1
    entry = update.await_args.args[2]
    assert entry["result"] == "miss"


@pytest.mark.asyncio
async def test_v2_neutral_grade_is_null():
    """G14：neutral 档不输出 grade（strong_hit 语义与 neutral 方向反转）。"""
    record = _pending_record(due="2026-08-10", direction="neutral")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": 0.2},   # |pct|<0.5 → hit
        {"trade_date": "2026-08-11", "pct_chg": 1.5},
        {"trade_date": "2026-08-12", "pct_chg": 2.0},
        {"trade_date": "2026-08-13", "pct_chg": -1.0},
    ]
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions", new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "get_index_kline", new=AsyncMock(return_value=kline_rows)),
        patch.object(prediction_validator.node_api, "update_prediction_verification", new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today", return_value=date(2026, 8, 13)),
    ):
        updated = await run_once()
    entry = update.await_args.args[2]
    assert entry["result"] == "hit"
    assert "grade" not in entry
