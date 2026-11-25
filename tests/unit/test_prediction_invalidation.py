from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.prediction import PredictionRisk
from aistock_agent.services import prediction_invalidation
from aistock_agent.services.prediction_invalidation import (
    scan_active_pending,
    update_trigger_state,
)

INIT = {"state": "inactive", "below_streak": 0, "above_streak": 0}


def test_trigger_requires_two_consecutive_below_days():
    state, s1 = update_trigger_state(INIT, True)
    assert state == "inactive"          # 第 1 日跌破，未达标
    state, _ = update_trigger_state(s1, True)
    assert state == "armed"             # 第 2 日跌破 → armed


def test_single_day_flip_does_not_advance():
    state, _ = update_trigger_state(INIT, True)
    state, _ = update_trigger_state(INIT, False)  # 单日回升
    assert state == "inactive"


def test_armed_enters_de_escalating_then_releases():
    _, s1 = update_trigger_state(INIT, True)
    _, s2 = update_trigger_state(s1, True)
    assert s2["state"] == "armed"
    state, s3 = update_trigger_state(s2, False)   # 收复第 1 日
    assert state == "de_escalating"
    state, s4 = update_trigger_state(s3, False)   # 收复第 2 日
    assert state == "de_escalating"
    state, s5 = update_trigger_state(s4, False)   # 收复第 3 日 → release
    assert state == "inactive"


def test_de_escalating_returns_to_armed_on_break():
    _, s1 = update_trigger_state(INIT, True)
    _, s2 = update_trigger_state(s1, True)
    _, s3 = update_trigger_state(s2, False)
    assert s3["state"] == "de_escalating"
    state, _ = update_trigger_state(s3, True)     # 重新跌破 → 回 armed
    assert state == "armed"


def test_prediction_risk_new_fields_optional():
    r = PredictionRisk(factor="f", invalidation="i")
    assert r.indicator is None and r.triggered is False
    r2 = PredictionRisk(factor="f", invalidation="i", indicator="ma20", triggered=True)
    assert r2.indicator == "ma20" and r2.triggered is True


def _pending_record(pid, *, verification=None):
    """真实 pending 记录形状（PredictionRecordRow）：prediction.horizons 为 list，
    顶层无 target_type/target_code，horizon 集合取 due_dates keys。"""
    return {
        "id": pid,
        "source_type": "market_trace",
        "source_id": f"review:2026-08-0{pid}",
        "schema_version": "2.0",
        "prediction": {
            "horizons": [
                {
                    "horizon": "short",
                    "target": "上证指数",
                    "direction": "bearish",
                    "metric_projection": "x",
                }
            ]
        },
        "verification": verification or {},
        "status": "pending",
        "due_dates": {"short": "2026-09-30"},
        "created_at": "2026-08-01T00:00:00Z",
    }


def _kline(closes):
    return [
        {"trade_date": f"2026-08-{(i % 28) + 1:02d}", "close": c}
        for i, c in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_scan_active_no_trigger_on_rising_market():
    """单边上行（收盘恒高于 MA20）→ 状态机不推进：无触发、无回写。"""
    record = _pending_record(1)
    closes = [100.0 + i for i in range(130)]
    with (
        patch.object(
            prediction_invalidation.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_invalidation.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=_kline(closes)),
        ),
        patch.object(
            prediction_invalidation.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
    ):
        triggered = await scan_active_pending()
    assert triggered == []
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_active_triggers_on_two_consecutive_breaks():
    """前一日已跌破（persisted inactive/below_streak=1）+ 今日再跌破 → armed，
    新触发返回 id（str），并回写早退状态。"""
    record = _pending_record(
        2,
        verification={
            "short": {"early_exit": {"state": "inactive", "below_streak": 1, "above_streak": 0}}
        },
    )
    closes = [100.0] * 128 + [50.0, 49.0]  # 最近 2 根收盘明显跌破 MA20
    with (
        patch.object(
            prediction_invalidation.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_invalidation.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=_kline(closes)),
        ),
        patch.object(
            prediction_invalidation.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 2}),
        ) as update,
    ):
        triggered = await scan_active_pending()
    assert triggered == ["2"]
    assert update.await_count == 1
    prediction_id, horizon, entry = update.await_args.args
    assert prediction_id == 2
    assert horizon == "short"
    assert entry["type"] == "early_exit"
    assert entry["early_exit"]["state"] == "armed"
    assert entry["early_exit"]["below_streak"] == 2
    assert "triggered_at" in entry["early_exit"]
