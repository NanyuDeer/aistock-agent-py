from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import prediction_validator
from aistock_agent.services import prediction_validator as pv
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
async def test_fetch_kline_window_index_preserves_none_rows():
    """H7：pct_chg=None 行保留占位（不得静默丢行错位）。"""
    rows = [
        {"trade_date": "2026-08-10", "pct_chg": None},  # 缺值占位
        {"trade_date": "2026-08-11", "pct_chg": 1.5},
    ]
    with patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)) as m:
        out = await pv._fetch_kline_window("index", "000001", "2026-08-10")
    assert out == [{"trade_date": "2026-08-10", "pct_chg": None},
                   {"trade_date": "2026-08-11", "pct_chg": 1.5}]
    # 必须携带区间参数（非 200 天滚动），且锁定 _range_around_due 区间数学：
    # due=2026-08-10 → [2026-08-10 减 20 天, 加 10 天] = [20260721, 20260820]
    _, kwargs = m.call_args
    assert kwargs["start_date"] == "20260721"
    assert kwargs["end_date"] == "20260820"


@pytest.mark.asyncio
async def test_run_once_h7_missing_pct_chg_rows_insufficient():
    """H7：kline 含 pct_chg=None 占位行 → 计数 >0 落 insufficient(subtype=no_data)，
    reason 含 'pct_chg 空'，不静默。"""
    record = _pending_record(due="2026-08-10")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": None},  # 缺值占位
        {"trade_date": "2026-08-11", "pct_chg": 1.5},
        {"trade_date": "2026-08-12", "pct_chg": 0.2},
        {"trade_date": "2026-08-13", "pct_chg": -0.1},
    ]
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=kline_rows),
        ),
        patch.object(
            prediction_validator.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        updated = await run_once()
    assert updated == 1
    entry = update.await_args.args[2]
    assert entry["result"] == "insufficient"
    assert entry["subtype"] == "no_data"
    assert "pct_chg 空" in entry["reason"]


@pytest.mark.asyncio
async def test_fetch_kline_window_malformed_due_returns_none():
    """脏 due_date（非 %Y-%m-%d）→ 窗口无法确定 → 返回 None（数据源故障语义），不抛异常。"""
    with patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=[])) as m:
        out = await pv._fetch_kline_window("index", "000001", "not-a-date")
    assert out is None
    m.assert_not_awaited()  # 窗口无法确定，不应发请求


@pytest.mark.asyncio
async def test_run_once_dirty_due_date_does_not_crash_batch():
    """脏 due_date 档位不得让整批验证崩溃：落 insufficient，其余记录正常回写。"""
    bad = _pending_record(record_id=1, due="")
    good = _pending_record(record_id=2, due="2026-08-10")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": 1.2},
        {"trade_date": "2026-08-11", "pct_chg": 0.3},
        {"trade_date": "2026-08-12", "pct_chg": -0.2},
        {"trade_date": "2026-08-13", "pct_chg": 0.1},
    ]
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[bad, good]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=kline_rows),
        ),
        patch.object(
            prediction_validator.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        updated = await run_once()
    assert updated == 2  # 脏档 insufficient + 正常档 hit，均回写
    by_id = {call.args[0]: call.args[2] for call in update.call_args_list}
    assert by_id[1]["result"] == "insufficient"  # 脏 due 档不崩溃、可追溯
    assert by_id[1]["reason"] == "指数行情不可用"
    assert by_id[2]["result"] == "hit"           # 正常档不受影响


@pytest.mark.asyncio
async def test_fetch_kline_window_sector_calls_ths_range():
    with patch.object(
        pv.node_api, "get_ths_daily_range",
        new=AsyncMock(return_value=[{"trade_date": "2026-08-10", "pct_chg": 0.5}]),
    ) as m:
        out = await pv._fetch_kline_window("sector", "885525.TI", "2026-08-10")
    assert out == [{"trade_date": "2026-08-10", "pct_chg": 0.5}]
    assert m.await_args.args[0] == "885525.TI"


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


def _pending_sector_record(due="2026-08-10", direction="bullish", target="半导体板块"):
    return _pending_record(due=due, direction=direction, target=target)


@pytest.mark.asyncio
async def test_verify_sector_target_resolves_and_hit():
    """H3/H8：板块 target resolve 命中 → 走 sector kline，entry 带 target_type/
    matched_ts_code/matched_name/threshold_version/prediction_id。"""
    record = _pending_sector_record(direction="bullish")
    kline = [{"trade_date": "2026-08-10", "pct_chg": 1.0},  # >0 hit
             {"trade_date": "2026-08-11", "pct_chg": 0.3},
             {"trade_date": "2026-08-12", "pct_chg": -0.2},
             {"trade_date": "2026-08-13", "pct_chg": -0.1}]
    with (
        patch.object(
            pv.node_api, "resolve_ths_name",
            new=AsyncMock(return_value={"ts_code": "881121.TI", "name": "半导体"}),
        ),
        patch.object(pv.node_api, "get_ths_daily_range", new=AsyncMock(return_value=kline)),
        patch.object(
            pv.node_api, "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            pv.node_api, "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        updated = await pv.run_once()
    assert updated == 1
    entry = update.await_args.args[2]
    assert entry["result"] == "hit"
    assert entry["target_type"] == "sector"
    assert entry["matched_ts_code"] == "881121.TI"
    assert entry["matched_name"] == "半导体"
    assert entry["threshold_version"] == "1.0"
    assert "prediction_id" in entry


@pytest.mark.asyncio
async def test_verify_sector_target_unresolved_is_no_source():
    """H2：板块 resolve 未命中 → insufficient/subtype=no_source，reason 含 '未匹配板块名'。"""
    record = _pending_sector_record(target="不存在的板块")
    with (
        patch.object(pv.node_api, "resolve_ths_name", new=AsyncMock(return_value=None)),
        patch.object(
            pv.node_api, "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            pv.node_api, "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        await pv.run_once()
    entry = update.await_args.args[2]
    assert entry["result"] == "insufficient"
    assert entry["subtype"] == "no_source"
    assert "未匹配板块名" in entry["reason"]


@pytest.mark.asyncio
async def test_sector_neutral_uses_sector_threshold():
    """H3：板块 neutral 阈值 0.25%（index 0.5% 复用会使命中率显著偏低——G0c 实证）。"""
    record = _pending_sector_record(direction="neutral")
    kline = [{"trade_date": "2026-08-10", "pct_chg": 0.3},  # |0.3|>0.25 → 非 neutral hit
             {"trade_date": "2026-08-11", "pct_chg": 0.4},
             {"trade_date": "2026-08-12", "pct_chg": -0.4},
             {"trade_date": "2026-08-13", "pct_chg": 0.35}]
    with (
        patch.object(
            pv.node_api, "resolve_ths_name",
            new=AsyncMock(return_value={"ts_code": "885525.TI", "name": "白酒概念"}),
        ),
        patch.object(pv.node_api, "get_ths_daily_range", new=AsyncMock(return_value=kline)),
        patch.object(
            pv.node_api, "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            pv.node_api, "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        await pv.run_once()
    entry = update.await_args.args[2]
    assert entry["result"] == "miss"  # 板块阈值下无 |pct|<0.25 日（index 0.5 阈值下为 hit）
