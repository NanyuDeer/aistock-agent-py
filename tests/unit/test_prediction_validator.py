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
            "get",
            new=AsyncMock(
                return_value={
                    "indices": [
                        {"index": "000001", "name": "上证指数", "price": 3600, "changePercent": 1.2}
                    ]
                }
            ),
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
    assert entry["actual"] == "+1.20%"


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
            "get",
            new=AsyncMock(
                return_value={
                    "indices": [
                        {
                            "index": "000001",
                            "name": "上证指数",
                            "price": 3600,
                            "changePercent": -0.8,
                        }
                    ]
                }
            ),
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
    assert entry["result"] == "miss"  # bullish 但当日 -0.8% 为跌 → miss（对照逻辑不变）
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
