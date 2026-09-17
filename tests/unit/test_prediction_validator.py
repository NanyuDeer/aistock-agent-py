from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import prediction_validator
from aistock_agent.services import prediction_validator as pv
from aistock_agent.services.prediction_validator import (
    _INDEX_CODE_MAP,
    _should_skip_horizon,
    run_once,
)


@pytest.fixture(autouse=True)
def _no_backfill_verified_records():
    """Task 10：run_once 内嵌 backfill_no_data 扫描 verified 记录；默认空扫描，
    避免既有 run_once 用例触发真实 HTTP；回补用例内显式覆盖该 patch。"""
    with patch.object(
        prediction_validator.node_api,
        "list_verified_predictions",
        new=AsyncMock(return_value=[]),
    ):
        yield


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
async def test_v3_verify_bullish_window_hit_with_grade():
    """3.0：bullish 档窗口累计 sum>0 → hit；due 当日未命中、窗口无 >=5% → 普通 hit。"""
    record = _pending_record(due="2026-08-10", direction="bullish")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": -0.5},  # due 当日（未命中）
        {"trade_date": "2026-08-11", "pct_chg": 1.8},   # 窗口累计正贡献
        {"trade_date": "2026-08-12", "pct_chg": 0.2},
        {"trade_date": "2026-08-13", "pct_chg": -0.1},
    ]  # sum=1.4 > 0
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
    assert entry["methodology_version"] == "3.0"
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
    assert out == [{"trade_date": "2026-08-10", "pct_chg": None, "close": None, "vol": None,
                    "amount": None},
                   {"trade_date": "2026-08-11", "pct_chg": 1.5, "close": None, "vol": None,
                    "amount": None}]
    # 必须携带区间参数（非 200 天滚动），且锁定 _range_around_due 区间数学：
    # due=2026-08-10 → [2026-08-10 减 20 天, 加 10 天] = [20260721, 20260820]
    _, kwargs = m.call_args
    assert kwargs["start_date"] == "20260721"
    assert kwargs["end_date"] == "20260820"


@pytest.mark.asyncio
async def test_fetch_kline_window_keeps_close_and_vol():
    """两段判定需要 close（技术位）与 vol（后续 volume 类）；缺失保 None 占位。"""
    rows = [
        {"trade_date": "20260916", "pct_chg": 1.2, "close": 3100.5, "vol": 2.1e8},
        {"trade_date": "20260917", "pct_chg": None, "close": None, "vol": None},
    ]
    with patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)):
        out = await pv._fetch_kline_window("index", "000001.SH", "2026-09-17")
    assert out is not None
    assert out[0]["close"] == 3100.5 and out[0]["vol"] == 2.1e8
    assert out[1]["close"] is None and out[1]["vol"] is None


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


@pytest.mark.asyncio
async def test_fetch_kline_window_stock_calls_quote_kline():
    """Spec B §4.5：个股走 get_stock_kline（/internal/quote/{code}/kline），
    且携带与指数一致的区间参数（[due-20, due+10]），返回归一化行。"""
    rows = [
        {"trade_date": "2026-08-10", "pct_chg": 1.5},
        {"trade_date": "2026-08-11", "pct_chg": 0.3},
    ]
    with patch.object(pv.node_api, "get_stock_kline", new=AsyncMock(return_value=rows)) as m:
        out = await pv._fetch_kline_window("stock", "600519", "2026-08-10")
    assert out == [{"trade_date": "2026-08-10", "pct_chg": 1.5, "close": None, "vol": None,
                    "amount": None},
                   {"trade_date": "2026-08-11", "pct_chg": 0.3, "close": None, "vol": None,
                    "amount": None}]
    _, kwargs = m.call_args
    assert kwargs["start_date"] == "20260721"
    assert kwargs["end_date"] == "20260820"


@pytest.mark.asyncio
async def test_resolve_verify_target_stock_code():
    """Spec B §4.5：6 位个股裸码 → (code, "stock", None)，绕过板块 resolve（不发网络请求）。"""
    with patch.object(
        prediction_validator,
        "resolve_sector_target",
        new=AsyncMock(return_value=None),
    ):
        code, target_type, matched = await pv._resolve_verify_target("600519")
    assert code == "600519"
    assert target_type == "stock"
    assert matched is None


@pytest.mark.asyncio
async def test_run_once_verifies_stock_horizon():
    """Spec B §4.5：个股 target 到期验证落地（target_type=stock，不再落 no_source）。"""
    record = _pending_record(due="2026-08-10", target="600519")
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_stock_kline",
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
    assert entry["target_type"] == "stock"
    assert entry["actual"] == "+1.40%"


@pytest.mark.asyncio
async def test_verify_conditions_skips_future_due_d6():
    """D6（2026-09-03）回归：condition 到期日仍在未来 → 不产 entry（未到验证窗口）。
    此前对未来 due（如 09-09）落 insufficient no_data，违反窗口语义。"""
    record = {
        "id": 1,
        "prediction": {
            "horizons": [{"horizon": "short", "target": "上证指数", "direction": "bullish"}],
            "conditions": [{
                "condition": "若未来 1 周累计上涨超 1%",
                "scenario": "后续 1-4 周上行",
                "anchor": {"horizon": "short", "direction": "bullish", "threshold": "+1%"},
            }],
        },
        "due_dates": {"short": "2026-09-09"},
        "verification": {},
    }
    with patch("aistock_agent.services.prediction_validator.shanghai_today",
               return_value=date(2026, 9, 3)):
        out = await pv._verify_conditions(record)
    assert out == {}


@pytest.mark.asyncio
async def test_verify_horizon_today_no_kline_waits_d6():
    """D6（2026-09-03）回归：due=今天但当日日 K 未出（盘中/收盘前）→ wait 而非
    insufficient——盘中跑写死 no_data 会被 _should_skip_horizon 拦下无法收盘后重判。"""
    record = _pending_record(due="2026-09-03", target="上证指数")
    with (
        patch.object(prediction_validator.node_api, "get_index_kline",
                     new=AsyncMock(return_value=[
                         {"trade_date": "2026-09-01", "pct_chg": 0.5},
                         {"trade_date": "2026-09-02", "pct_chg": -0.3},
                     ])),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 3)),
    ):
        entry = await pv._verify_horizon(record, "mid")
    assert entry.get("wait") is True
    assert entry.get("result") is None


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
async def test_fetch_kline_window_normalizes_yyyymmdd_trade_date():
    """Task 11 smoke 修复：Node 端 kline 返回 YYYYMMDD trade_date（Tushare 原始格式），
    必须归一化为 YYYY-MM-DD 才能与 due_date 精确匹配；已归一化的行幂等透传。"""
    rows = [
        {"trade_date": "20260810", "pct_chg": 1.2},  # YYYYMMDD → 归一化
        {"trade_date": "2026-08-11", "pct_chg": 0.3},  # 已是 YYYY-MM-DD → 透传
        {"trade_date": "20260812", "pct_chg": -0.2},
    ]
    with patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)):
        out = await pv._fetch_kline_window("index", "000001", "2026-08-10")
    assert out == [
        {"trade_date": "2026-08-10", "pct_chg": 1.2, "close": None, "vol": None, "amount": None},
        {"trade_date": "2026-08-11", "pct_chg": 0.3, "close": None, "vol": None, "amount": None},
        {"trade_date": "2026-08-12", "pct_chg": -0.2, "close": None, "vol": None, "amount": None},
    ]


@pytest.mark.asyncio
async def test_verify_horizon_yyyymmdd_trade_date_matches_due():
    """回归（存量 no_data 根因）：Node 端 YYYYMMDD trade_date 行 + YYYY-MM-DD due_date
    → 到期日精确匹配成功，结果 hit（此前恒落 no_data）。"""
    record = _pending_record(due="2026-08-10", direction="bullish")
    kline_rows = [
        {"trade_date": "20260810", "pct_chg": 1.2},  # due 当日 +1.2% → hit, strong_hit
        {"trade_date": "20260811", "pct_chg": 0.3},
        {"trade_date": "20260812", "pct_chg": -0.2},
        {"trade_date": "20260813", "pct_chg": 0.1},
    ]
    with (
        patch.object(
            pv.node_api, "get_index_kline", new=AsyncMock(return_value=kline_rows),
        ),
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        entry = await pv._verify_horizon(record, "mid")
    assert entry["result"] == "hit"  # 不再 no_data
    assert entry["grade"] == "strong_hit"
    assert entry["actual"] == "+1.40%"


@pytest.mark.asyncio
async def test_verify_horizon_sector_yyyymmdd_matches_due():
    """回归（sector 分支）：ths daily 同样 YYYYMMDD → 归一化后到期日匹配成功。"""
    record = _pending_sector_record(due="2026-08-10", direction="bullish")
    kline_rows = [
        {"trade_date": "20260810", "pct_chg": 1.0},
        {"trade_date": "20260811", "pct_chg": 0.3},
        {"trade_date": "20260812", "pct_chg": -0.2},
        {"trade_date": "20260813", "pct_chg": -0.1},
    ]
    with (
        patch.object(
            pv.node_api, "resolve_ths_name",
            new=AsyncMock(return_value={"ts_code": "881121.TI", "name": "半导体"}),
        ),
        patch.object(
            pv.node_api, "get_ths_daily_range", new=AsyncMock(return_value=kline_rows),
        ),
        patch(
            "aistock_agent.services.prediction_validator.shanghai_today",
            return_value=date(2026, 8, 13),
        ),
    ):
        entry = await pv._verify_horizon(record, "mid")
    assert entry["result"] == "hit"  # 不再 no_data
    assert entry["target_type"] == "sector"


@pytest.mark.asyncio
async def test_fetch_kline_window_sector_calls_ths_range():
    with patch.object(
        pv.node_api, "get_ths_daily_range",
        new=AsyncMock(return_value=[{"trade_date": "2026-08-10", "pct_chg": 0.5}]),
    ) as m:
        out = await pv._fetch_kline_window("sector", "885525.TI", "2026-08-10")
    assert out == [{"trade_date": "2026-08-10", "pct_chg": 0.5, "close": None, "vol": None,
                    "amount": None}]
    assert m.await_args.args[0] == "885525.TI"


@pytest.mark.asyncio
async def test_v3_neutral_grade_is_null():
    """G14：neutral 档不输出 grade（strong_hit 语义与 neutral 方向反转）。"""
    record = _pending_record(due="2026-08-10", direction="neutral")
    kline_rows = [
        {"trade_date": "2026-08-10", "pct_chg": 0.2},   # mean(|p|)=0.3 < 0.5 → hit
        {"trade_date": "2026-08-11", "pct_chg": 0.4},
        {"trade_date": "2026-08-12", "pct_chg": -0.3},
        {"trade_date": "2026-08-13", "pct_chg": 0.3},
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
    assert entry["methodology_version"] == "3.0"
    assert "grade" not in entry


def _pending_sector_record(due="2026-08-10", direction="bullish", target="半导体板块"):
    return _pending_record(due=due, direction=direction, target=target)


def _verified_no_data_record(
    record_id=1, due="2026-08-10", target="上证指数", direction="bullish"
):
    """存量 verified 记录：verification 含 2.0/no_data 的 index 档（D4 回补目标）。"""
    return {
        "id": record_id,
        "prediction": {
            "horizons": [
                {
                    "horizon": "short",
                    "target": target,
                    "direction": direction,
                    "metric_projection": "x",
                }
            ]
        },
        "due_dates": {"short": due},
        "verification": {
            "short": {
                "result": "insufficient",
                "subtype": "no_data",
                "target_type": "index",
                "methodology_version": "2.0",
            }
        },
    }


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


@pytest.mark.asyncio
async def test_backfill_no_data_retries_index_with_range():
    """D4 回补：存量 2.0/no_data 的 index 档按 due 区间重验，覆盖回写 hit。"""
    record = _verified_no_data_record()
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 1.2},  # due 当日 +1.2% → hit
        {"trade_date": "2026-08-11", "pct_chg": 0.3},
        {"trade_date": "2026-08-12", "pct_chg": -0.2},
        {"trade_date": "2026-08-13", "pct_chg": 0.1},
    ]
    with (
        patch.object(
            prediction_validator.node_api,
            "list_verified_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=kline),
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
        updated = await pv.backfill_no_data()
    assert updated == 1
    assert update.await_args.args[0] == 1  # 覆盖原记录 id
    entry = update.await_args.args[2]
    assert entry["result"] == "hit"


@pytest.mark.asyncio
async def test_backfill_no_data_skips_still_insufficient():
    """D4 幂等：重验仍 insufficient → 不覆盖回写。"""
    record = _verified_no_data_record()
    with (
        patch.object(
            prediction_validator.node_api,
            "list_verified_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=None),  # 数据源仍不可用
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
        updated = await pv.backfill_no_data()
    assert updated == 0
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_no_data_skips_non_no_data_entries():
    """D4 幂等：hit/miss 档不回补（仅 2.0/no_data/insufficient 档重验）。"""
    record = _verified_no_data_record()
    record["verification"]["short"]["result"] = "hit"
    with (
        patch.object(
            prediction_validator.node_api,
            "list_verified_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api, "get_index_kline", new=AsyncMock()
        ) as kline,
        patch.object(
            prediction_validator.node_api,
            "update_prediction_verification",
            new=AsyncMock(return_value={"id": 1}),
        ) as update,
    ):
        updated = await pv.backfill_no_data()
    assert updated == 0
    kline.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_backfill_runs_when_no_pending():
    """D4：无 pending 记录（早退路径）时 backfill 仍执行（resolution 1）。"""
    record = _verified_no_data_record()
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 1.2},
        {"trade_date": "2026-08-11", "pct_chg": 0.3},
        {"trade_date": "2026-08-12", "pct_chg": -0.2},
        {"trade_date": "2026-08-13", "pct_chg": 0.1},
    ]
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            prediction_validator.node_api,
            "list_verified_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=kline),
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
    assert updated == 0  # 主链路无新增；回补数不入返回值（resolution 4）
    assert update.await_count == 1
    assert update.await_args.args[2]["result"] == "hit"


def test_early_exit_only_entry_does_not_skip():
    """A1：early_exit-only 状态 dict（无 result）不阻塞到期验证。"""
    entry = {"early_exit": {"state": "armed"}}
    assert _should_skip_horizon(entry) is False


def test_result_entry_skips():
    """A1：已含 result（hit/miss/insufficient）的档位跳过到期验证。"""
    entry = {"result": "hit"}
    assert _should_skip_horizon(entry) is True


# ============ 阶段 0：3.0 窗口累计主判 ============

def _verify_direct(record, horizon="mid", methodology_version="3.0", kline_rows=None):
    """直接调 _verify_horizon（不经 run_once），mock kline + 今日。"""
    import aistock_agent.services.prediction_validator as pv

    async def _run():
        with (
            patch.object(
                pv.node_api, "get_index_kline", new=AsyncMock(return_value=kline_rows),
            ),
            patch(
                "aistock_agent.services.prediction_validator.shanghai_today",
                return_value=date(2026, 8, 13),
            ),
        ):
            return await pv._verify_horizon(record, horizon, methodology_version=methodology_version)

    return _run()


@pytest.mark.asyncio
async def test_v3_bullish_cumulative_sum_positive_hit():
    """3.0：bullish 窗口累计 sum>0 → hit；entry 写 methodology_version='3.0'。"""
    record = _pending_record(due="2026-08-10", direction="bullish")
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 1.0},   # due 当日 +1.0% → strong_hit
        {"trade_date": "2026-08-11", "pct_chg": 0.5},
        {"trade_date": "2026-08-12", "pct_chg": -0.5},
        {"trade_date": "2026-08-13", "pct_chg": -0.2},
    ]  # sum=0.8 > 0
    entry = await _verify_direct(record, kline_rows=kline)
    assert entry["result"] == "hit"
    assert entry["grade"] == "strong_hit"  # due 当日命中
    assert entry["methodology_version"] == "3.0"
    assert entry["actual"] == "+0.80%"


@pytest.mark.asyncio
async def test_v3_bullish_any_positive_but_cumulative_negative_is_miss():
    """3.0 反例：bullish 单日 >0 但窗口累计 <=0 → miss（2.0 的 any>0 为 hit，口径真变化）。"""
    record = _pending_record(due="2026-08-10", direction="bullish")
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 1.0},   # 单日 +1.0% 但被大跌淹没
        {"trade_date": "2026-08-11", "pct_chg": -2.0},
        {"trade_date": "2026-08-12", "pct_chg": -1.0},
        {"trade_date": "2026-08-13", "pct_chg": -0.5},
    ]  # sum=-2.5 <= 0
    entry = await _verify_direct(record, kline_rows=kline)
    assert entry["result"] == "miss"
    # 3.0 miss 且窗口无 >=5% 反向幅度（-2.0 < 5.0）→ 普通 miss（非 strong_miss）
    assert entry["grade"] == "miss"


@pytest.mark.asyncio
async def test_v3_bearish_any_negative_but_cumulative_positive_is_miss():
    """3.0 反例：bearish 单日 <0 但窗口累计 >=0 → miss（2.0 any<0 为 hit）。"""
    record = _pending_record(due="2026-08-10", direction="bearish")
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": -1.0},
        {"trade_date": "2026-08-11", "pct_chg": 2.0},
        {"trade_date": "2026-08-12", "pct_chg": 1.0},
        {"trade_date": "2026-08-13", "pct_chg": 0.5},
    ]  # sum=2.5 >= 0
    entry = await _verify_direct(record, kline_rows=kline)
    assert entry["result"] == "miss"
    assert "grade" in entry  # bearish 输出 grade（strong_miss/hit）


@pytest.mark.asyncio
async def test_v3_neutral_mean_abs_below_threshold_hit():
    """3.0：neutral 主判 = mean(|p_i|) < 0.5 → hit（不再要求单日 |p|<0.5）。"""
    record = _pending_record(due="2026-08-10", direction="neutral")
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 0.4},
        {"trade_date": "2026-08-11", "pct_chg": -0.4},
        {"trade_date": "2026-08-12", "pct_chg": 0.4},
        {"trade_date": "2026-08-13", "pct_chg": 0.3},
    ]  # mean(|p|)=0.375 < 0.5
    entry = await _verify_direct(record, kline_rows=kline)
    assert entry["result"] == "hit"
    assert "grade" not in entry  # G14：neutral 恒不输出 grade


@pytest.mark.asyncio
async def test_v3_neutral_mean_abs_above_threshold_is_miss():
    """3.0 反例：neutral 有单日 |p|<0.5 但 mean(|p_i|)>=0.5 → miss（2.0 any 口径为 hit）。"""
    record = _pending_record(due="2026-08-10", direction="neutral")
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 0.1},   # |0.1|<0.5 单日横盘
        {"trade_date": "2026-08-11", "pct_chg": 2.0},
        {"trade_date": "2026-08-12", "pct_chg": -1.0},
        {"trade_date": "2026-08-13", "pct_chg": 1.0},
    ]  # mean(|p|)=1.025 >= 0.5
    entry = await _verify_direct(record, kline_rows=kline)
    assert entry["result"] == "miss"


@pytest.mark.asyncio
async def test_v3_vs_v2_baseline_neutral_differs():
    """baseline_neutral 随版本：同一窗口 {0.3,2.0,-1.0,1.0}（thr=0.5）——
    v2 any(|p|<0.5)=True；v3 mean(|p|)=1.075 → False。"""
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 0.3},
        {"trade_date": "2026-08-11", "pct_chg": 2.0},
        {"trade_date": "2026-08-12", "pct_chg": -1.0},
        {"trade_date": "2026-08-13", "pct_chg": 1.0},
    ]
    record = _pending_record(due="2026-08-10", direction="neutral")
    e_v3 = await _verify_direct(record, methodology_version="3.0", kline_rows=kline)
    e_v2 = await _verify_direct(record, methodology_version="2.0", kline_rows=kline)
    assert e_v3["methodology_version"] == "3.0"
    assert e_v2["methodology_version"] == "2.0"
    assert e_v3["baseline_neutral"] is False   # mean(|p|)=1.075 >= 0.5
    assert e_v2["baseline_neutral"] is True    # any(0.3 < 0.5)
    assert e_v3["result"] == "miss"            # mean>=0.5 → 3.0 miss
    assert e_v2["result"] == "hit"             # any|p|<0.5 → 2.0 hit


@pytest.mark.asyncio
async def test_backfill_no_data_rewrites_keep_v2_version():
    """阶段 0：backfill 重验存量 2.0/no_data 记录——用 2.0 口径、写回 methodology_version='2.0'（不混版本）。"""
    record = _verified_no_data_record()
    kline = [
        {"trade_date": "2026-08-10", "pct_chg": 1.0},   # 单日 +1.0%（2.0 any>0 → hit）
        {"trade_date": "2026-08-11", "pct_chg": -2.0},  # 但 3.0 累计 sum=-2.5 → miss
        {"trade_date": "2026-08-12", "pct_chg": -1.0},
        {"trade_date": "2026-08-13", "pct_chg": -0.5},
    ]
    with (
        patch.object(
            prediction_validator.node_api,
            "list_verified_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_index_kline",
            new=AsyncMock(return_value=kline),
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
        updated = await pv.backfill_no_data()
    assert updated == 1
    entry = update.await_args.args[2]
    # backfill 保持 2.0 口径：bullish any>0（单日 +1.0%）→ hit；若误用 3.0 累计 sum=-2.5 → miss
    assert entry["result"] == "hit"
    assert entry["methodology_version"] == "2.0"


def _verified_rec(target, entries):
    """构造一条 verified 记录（指定 target + verification entries）。"""
    return {
        "id": target,
        "prediction": {"horizons": [{"horizon": "mid", "target": target,
                                     "direction": "bullish"}]},
        "verification": {str(i): e for i, e in enumerate(entries)},
    }


@pytest.mark.asyncio
async def test_write_validation_profiles_groups_by_target():
    """Spec B §7 P4：run_once 接管——按 target 落画像缓存（key 用稳定 internal_id）。"""
    # stock target（600519）与 index target（000001 -> 000001.SH code）
    records = [
        _verified_rec("600519", [
            {"result": "hit", "methodology_version": "3.0", "target_type": "stock"},
            {"result": "miss", "methodology_version": "3.0", "target_type": "stock"},
        ]),
        _verified_rec("上证指数", [
            {"result": "hit", "methodology_version": "3.0", "target_type": "index"},
        ]),
    ]
    written: dict[str, object] = {}
    async def _set(key, profile, ttl=None):
        written[key] = profile
        return True
    with (
        patch.object(prediction_validator.node_api, "list_verified_predictions",
                     new=AsyncMock(return_value=records)),
        patch.object(prediction_validator, "set_cached_validation_profile",
                     new=_set),
    ):
        n = await pv._write_validation_profiles()
    assert n == 2
    # key = _resolve_verify_target 收敛的稳定 code（stock 裸码 / index 裸码，不加交易所后缀）
    assert set(written) == {"600519", "000001"}
    assert written["600519"]["n"] == 2 and written["600519"]["hit_rate"] == 0.5
    assert written["000001"]["n"] == 1 and written["000001"]["hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_write_validation_profiles_skips_early_exit_no_result():
    """Spec B §7 P4：early_exit-only（无 result）不入画像；无 target 的记录被跳过。"""
    records = [
        _verified_rec("600519", [
            {"meaning": "early_exit", "horizon": "mid"},  # 无 result → 不计入
            {"result": "hit", "methodology_version": "3.0", "target_type": "stock"},
        ]),
        {"id": 9, "prediction": {"horizons": []}, "verification": {"h": {"result": "miss"}}},
    ]
    written: dict[str, object] = {}
    async def _set(key, profile, ttl=None):
        written[key] = profile
        return True
    with (
        patch.object(prediction_validator.node_api, "list_verified_predictions",
                     new=AsyncMock(return_value=records)),
        patch.object(prediction_validator, "set_cached_validation_profile",
                     new=_set),
    ):
        n = await pv._write_validation_profiles()
    assert n == 1
    assert written["600519"]["n"] == 1  # 只算带 result 的 hit
    assert "9" not in written  # horizons 空 → 无 target → 跳过


@pytest.mark.asyncio
async def test_write_validation_profiles_scans_pending_records_d3():
    """D3（2026-09-03）回归：画像数据源改为档位级扫描（list_all = pending+verified）。

    status=pending 但已有 result 档位的记录必须计入画像——不再等全档 verified
    （long 2027 才到期），short/mid 到期写入即计入。
    """
    pending_rec = {
        "id": 1001, "status": "pending",
        "prediction": {"horizons": [
            {"horizon": "short", "target": "600519", "direction": "bullish"},
            {"horizon": "mid", "target": "600519", "direction": "bullish"},
        ]},
        # short 已写 hit；mid 未到期无 result → 不计入
        "verification": {"short": {"result": "hit", "methodology_version": "3.0",
                                   "target_type": "stock"}},
    }
    verified_rec = _verified_rec("上证指数", [
        {"result": "miss", "methodology_version": "3.0", "target_type": "index"},
    ])
    written: dict[str, object] = {}
    async def _set(key, profile, ttl=None):
        written[key] = profile
        return True
    with (
        patch.object(prediction_validator.node_api, "list_all_predictions",
                     new=AsyncMock(return_value=[pending_rec, verified_rec])),
        patch.object(prediction_validator, "set_cached_validation_profile",
                     new=_set),
    ):
        n = await pv._write_validation_profiles()
    assert n == 2
    assert written["600519"]["n"] == 1 and written["600519"]["hit_rate"] == 1.0
    assert written["000001"]["n"] == 1 and written["000001"]["hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_run_once_writes_profile_after_verification():
    """Spec B §7 P4：run_once 收尾调用 _write_validation_profiles（结果成功回写后落画像）。"""
    record = _pending_record(due="2026-08-10", target="600519")
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions",
                     new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "list_verified_predictions",
                     new=AsyncMock(side_effect=[[], [{  # backfill 空 + 画像窗口含 600519 hit
                         "id": 1,
                         "prediction": {"horizons": [{"target": "600519"}]},
                         "verification": {"h": {"result": "hit", "methodology_version": "3.0",
                                                "target_type": "stock"}},
                     }]])),
        patch.object(prediction_validator.node_api, "get_stock_kline",
                     new=AsyncMock(return_value=[
                         {"trade_date": "2026-08-10", "pct_chg": 1.2},
                         {"trade_date": "2026-08-11", "pct_chg": 0.3},
                         {"trade_date": "2026-08-12", "pct_chg": -0.2},
                         {"trade_date": "2026-08-13", "pct_chg": 0.1},
                     ])),
        patch.object(prediction_validator.node_api, "update_prediction_verification",
                     new=AsyncMock(return_value={"id": 1})),
        patch.object(prediction_validator, "set_cached_validation_profile",
                     new=AsyncMock(return_value=True)) as setp,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 8, 10)),
    ):
        updated = await run_once()
    assert updated == 1
    assert setp.await_count == 1
    key = setp.await_args.args[0]
    assert key == "600519"


@pytest.mark.asyncio
async def test_run_once_normalizes_string_record_id_d2():
    """D2（2026-09-03）回归：Node internal 曾返回 string id（pg BIGSERIAL）致
    isinstance(record_id, int) 门禁全量跳过（updated=0 堆积）；现在兼容 string，
    归一 int 后正常回写验证。"""
    record = _pending_record(record_id="35", due="2026-08-10", target="600519")
    captured: list[tuple[int, str, dict[str, object]]] = []

    async def _update(
        prediction_id: int, horizon: str, entry: dict[str, object]
    ) -> dict[str, object]:
        captured.append((prediction_id, horizon, entry))
        return {"id": prediction_id}

    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions",
                     new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "list_verified_predictions",
                     new=AsyncMock(side_effect=[[], []])),  # backfill 空 + 画像窗口空
        patch.object(prediction_validator.node_api, "get_stock_kline",
                     new=AsyncMock(return_value=[
                         {"trade_date": "2026-08-10", "pct_chg": 1.2},
                         {"trade_date": "2026-08-11", "pct_chg": 0.3},
                         {"trade_date": "2026-08-12", "pct_chg": -0.2},
                         {"trade_date": "2026-08-13", "pct_chg": 0.1},
                     ])),
        patch.object(prediction_validator.node_api, "update_prediction_verification",
                     new=_update),
        patch.object(prediction_validator, "set_cached_validation_profile",
                     new=AsyncMock(return_value=True)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 8, 10)),
    ):
        updated = await run_once()
    assert updated == 1
    pid, horizon, entry = captured[0]
    assert pid == 35 and isinstance(pid, int)  # string id 已归一为 int
    assert horizon == "mid"
    assert entry["result"] == "hit"


# --- light_predict 下游就绪：带交易所后缀 ts_code 归一化（个股验证环） ---


def test_resolve_index_or_stock_suffixed_stock_code():
    """带后缀 ts_code（600519.SH / 000001.SZ）→ 个股裸码（个股 light_predict 通道）。"""
    code, target_type = pv._resolve_index_or_stock("600519.SH")
    assert code == "600519"
    assert target_type == "stock"
    code, target_type = pv._resolve_index_or_stock("000001.SZ")  # 平安银行，非上证指数
    assert code == "000001"
    assert target_type == "stock"


def test_resolve_index_or_stock_suffixed_index_code():
    """带后缀指数 ts_code（000001.SH 上证 / 000300.SH 沪深300 / 399006.SZ 创业板指）→ index。"""
    code, target_type = pv._resolve_index_or_stock("000001.SH")
    assert code == "000001"
    assert target_type == "index"
    code, target_type = pv._resolve_index_or_stock("399006.SZ")
    assert code == "399006"
    assert target_type == "index"


def test_resolve_index_or_stock_bare_forms():
    """裸码/别名仍走 index 优先、6 位裸码 stock、板块/抽象词留待 resolve。"""
    code, target_type = pv._resolve_index_or_stock("上证指数")
    assert code == "000001" and target_type == "index"
    code, target_type = pv._resolve_index_or_stock("600519")
    assert code == "600519" and target_type == "stock"
    code, target_type = pv._resolve_index_or_stock("存储板块")
    assert code is None and target_type == "sector"
    code, target_type = pv._resolve_index_or_stock("随便写")
    assert code is None and target_type == "unknown"


@pytest.mark.asyncio
async def test_resolve_verify_target_suffixed_stock_skips_sector_resolve():
    """带后缀个股 ts_code → 直接 stock，不发板块 resolve 网络请求。"""
    with patch.object(
        prediction_validator,
        "resolve_sector_target",
        new=AsyncMock(return_value={"ts_code": "BK0000", "name": "x"}),
    ) as mock_resolve:
        code, target_type, matched = await pv._resolve_verify_target("600519.SH")
    assert code == "600519"
    assert target_type == "stock"
    assert matched is None
    mock_resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_verifies_suffixed_stock_horizon():
    """light_predict 通道（target=带后缀 ts_code）到期验证落地 hit，不再 no_source。"""
    record = _pending_record(due="2026-08-10", target="600519.SH")
    with (
        patch.object(
            prediction_validator.node_api,
            "list_pending_predictions",
            new=AsyncMock(return_value=[record]),
        ),
        patch.object(
            prediction_validator.node_api,
            "get_stock_kline",
            new=AsyncMock(return_value=[
                {"trade_date": "2026-08-10", "pct_chg": 1.2},
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
    assert entry["target_type"] == "stock"
    assert entry["actual"] == "+1.40%"


# ============ condition_met 两段判定（T4 点亮 / T5 保留 / T6 接线） ============


def _pending_condition_record(
    record_id=1,
    due="2026-09-30",
    direction="bearish",
    condition="若跌破 MA20",
    threshold="",
    verification=None,
):
    """条件化 pending 记录：conditions[0].anchor.horizon='short' → due_dates['short']。"""
    return {
        "id": record_id,
        "source_type": "market_trace",
        "source_id": "review:2026-09-16",
        "prediction": {
            "horizons": [{"horizon": "short", "target": "上证指数",
                          "direction": direction, "metric_projection": "x"}],
            "conditions": [{
                "condition": condition,
                "scenario": "后续 1-4 周下行",
                "anchor": {"horizon": "short", "direction": direction,
                           "threshold": threshold},
            }],
        },
        "due_dates": {"short": due},
        "verification": verification if verification is not None else {},
    }


def _scan_rows(closes, pct_chg=-1.0, vol=1e8):
    """升序日 K 行（trade_date 与条件扫描判定无关：scan 不按 due 截断窗口）。"""
    return [
        {"trade_date": f"2026-09-{i + 1:02d}", "pct_chg": pct_chg,
         "close": float(c), "vol": vol}
        for i, c in enumerate(closes)
    ]


@pytest.mark.asyncio
async def test_scan_condition_met_lights_up_when_met() -> None:
    """T4 第①段：未到期但条件已成立（末日跌破 MA20）→ 产 condition_met=True entry。"""
    record = _pending_condition_record(due="2026-09-30", direction="bearish")
    rows = _scan_rows([130.0 - i for i in range(25)])  # 末日 106 < MA20 115.5
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out["c0"]["condition_met"] is True
    assert "result" not in out["c0"]  # 第①段只点亮，不写 result
    assert out["c0"]["condition_index"] == 0
    assert out["c0"]["horizon"] == "short"  # D5：anchor 档位（data_client → anchor_horizon）
    assert out["c0"]["target_type"] == "index"
    assert out["c0"]["prediction_id"] == 1
    assert out["c0"]["verified_at"] == "2026-09-16"


@pytest.mark.asyncio
async def test_scan_condition_met_no_entry_when_not_met() -> None:
    """T4 第①段：条件不成立 → 不产 entry（只写 true，不写 false，D1）。"""
    record = _pending_condition_record(
        due="2026-09-30", direction="bullish", condition="若站上 MA20")
    rows = _scan_rows([130.0 - i for i in range(25)])  # 下行 → 未站上 MA20
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out == {}


@pytest.mark.asyncio
async def test_scan_condition_met_skips_when_already_true() -> None:
    """T4 幂等：已有 condition_met=true → 跳过（不重复点亮，且不拉行情）。"""
    record = _pending_condition_record(
        record_id=7, verification={"c0": {"condition_met": True}})
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock()) as kline,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out == {}
    kline.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_condition_met_skips_due_and_result_entries() -> None:
    """T4：已到期（due<=today，交 stage②）与已到期末判定（有 result）的 c{i} 都不产 entry。"""
    due_record = _pending_condition_record(due="2026-09-16")  # due == today
    result_record = _pending_condition_record(verification={"c0": {"result": "hit"}})
    rows = _scan_rows([130.0 - i for i in range(25)])
    with (
        patch.object(pv.node_api, "get_index_kline",
                     new=AsyncMock(return_value=rows)) as kline,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        assert await pv._scan_condition_met(due_record) == {}
        assert await pv._scan_condition_met(result_record) == {}
    kline.assert_not_awaited()  # 两条都在取数前短路


@pytest.mark.asyncio
async def test_verify_conditions_preserves_lit_condition_met() -> None:
    """T5：stage① 已点亮 true → stage② 到期判定不得覆盖为 null。"""
    record = _pending_condition_record(
        due="2026-09-09", direction="bearish",
        verification={"c0": {"condition_met": True, "condition_index": 0}},
    )
    rows = _scan_rows([100.0 + i for i in range(25)])
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._verify_conditions(record)
    assert out["c0"]["result"] == "hit"          # 到期判定照常写 result
    assert out["c0"]["condition_met"] is True    # 但点亮状态被保留


@pytest.mark.asyncio
async def test_verify_conditions_writes_false_when_not_lit() -> None:
    """Task 6.1（spec §12.5）：到期对**确定性未成立**的条件写 condition_met=false + checked_at。

    与第①段的 true 形成完整布尔；checked_at 记录到期判定时间（便于审计与回溯区分）。
    """
    record = _pending_condition_record(due="2026-09-09", direction="bearish")
    rows = _scan_rows([100.0 + i for i in range(25)])  # 上行 → 未跌破 MA20 → 确定性不成立
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._verify_conditions(record)
    assert out["c0"]["result"] == "hit"           # scenario 命中（pct_chg 恒 -1）与条件成立无关
    assert out["c0"]["condition_met"] is False    # 到期未成立态
    assert out["c0"]["checked_at"] == "2026-09-16"
    assert out["c0"]["condition_index"] == 0


@pytest.mark.asyncio
async def test_verify_conditions_lit_true_not_downgraded_to_false() -> None:
    """Task 6.1 不可回退：已点亮 true 的条件到期**不得**被改写成 false（显式防御）。"""
    record = _pending_condition_record(
        due="2026-09-09", direction="bearish",
        verification={"c0": {"condition_met": True, "condition_index": 0}},
    )
    rows = _scan_rows([100.0 + i for i in range(25)])  # 判定口径会得 False，但已点亮必须保留
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._verify_conditions(record)
    assert out["c0"]["condition_met"] is True
    assert "checked_at" not in out["c0"]  # 不回退即不产生新的到期判定留痕


@pytest.mark.asyncio
async def test_verify_conditions_omits_condition_met_when_unjudgeable() -> None:
    """Task 6.1：无法判定（参考位降级）→ **不写该键**（保持缺失，绝不写 null）。"""
    record = {
        "id": 1,
        "prediction": {
            "horizons": [{"horizon": "short", "target": "上证指数", "direction": "bearish"}],
            "conditions": [{
                "condition": "若跌破今日盘中低点",
                "scenario": "后续继续下行",
                "anchor": {"horizon": "short", "direction": "bearish",
                           "threshold": "", "metric": "today_low", "op": "below"},
            }],
        },
        "due_dates": {"short": "2026-09-09"},
        "verification": {},
    }
    rows = _scan_rows([100.0 + i for i in range(25)])
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._verify_conditions(record)
    assert out["c0"]["result"] == "hit"
    assert "condition_met" not in out["c0"]
    assert "checked_at" not in out["c0"]


@pytest.mark.asyncio
async def test_run_once_condition_verify_idempotent_no_repeat_side_effects() -> None:
    """Task 6.1 幂等：重复到期扫描（c{i} 已有 result）不产生任何重复副作用（不重复回写）。"""
    record = _pending_condition_record(
        due="2026-09-09", direction="bearish",
        verification={
            "short": {"result": "hit"},
            "c0": {"result": "hit", "condition_met": False,
                   "condition_index": 0, "checked_at": "2026-09-16"},
        },
    )
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions",
                     new=AsyncMock(return_value=[record])),
        # D4 存量回补置空：本用例只验到期重扫的幂等（回补是独立路径）
        patch.object(prediction_validator.node_api, "list_verified_predictions",
                     new=AsyncMock(return_value=[])),
        patch.object(prediction_validator.node_api, "get_index_kline",
                     new=AsyncMock()) as kline,
        patch.object(prediction_validator.node_api, "update_prediction_verification",
                     new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        updated = await run_once()
    assert updated == 0
    update.assert_not_awaited()  # 副作用（回写）为零：重复扫描不重复写
    # 注：stage② 对已产 result 的 c{i} 仍会走一次 scenario 取数（既有行为，不写库），
    # Task 6.1 新加的到期未成立态判定在"已有 result"时提前跳过，不额外取数。
    assert kline.await_count <= 1


@pytest.mark.asyncio
async def test_run_once_scans_condition_met_before_due() -> None:
    """T6 端到端：due 在未来但条件已成立的 pending 记录 → 回写 (id, 'c0', 点亮 entry)。"""
    record = _pending_condition_record(due="2026-09-30", direction="bearish")
    rows = _scan_rows([130.0 - i for i in range(25)])
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions",
                     new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "get_index_kline",
                     new=AsyncMock(return_value=rows)),
        patch.object(prediction_validator.node_api, "update_prediction_verification",
                     new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        updated = await run_once()
    assert updated == 1
    update.assert_awaited_once()
    pid, ckey, entry = update.await_args.args
    assert (pid, ckey) == (1, "c0")
    assert entry["condition_met"] is True
    assert "result" not in entry


@pytest.mark.asyncio
async def test_run_once_does_not_rewrite_already_lit_condition() -> None:
    """T6 幂等：已点亮 true 的 c0 → run_once 不重复回写、不拉行情。"""
    record = _pending_condition_record(
        due="2026-09-30", direction="bearish",
        verification={"c0": {"condition_met": True}},
    )
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions",
                     new=AsyncMock(return_value=[record])),
        patch.object(prediction_validator.node_api, "get_index_kline",
                     new=AsyncMock()) as kline,
        patch.object(prediction_validator.node_api, "update_prediction_verification",
                     new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        updated = await run_once()
    assert updated == 0
    update.assert_not_awaited()
    kline.assert_not_awaited()


# ============ Task 5.1：anchor.metric/op/level 分流（量类 / 参考位 / 事件状态锚） ============


def _anchor_condition_record(anchor_extra: dict, condition: str, record_id: int = 1) -> dict:
    """带扩展 anchor 字段的 pending 记录（量类 / 参考位 / 事件类共用）。"""
    record = _pending_condition_record(
        record_id=record_id, due="2026-09-30", condition=condition)
    record["prediction"]["conditions"][0]["anchor"].update(anchor_extra)
    return record


@pytest.mark.asyncio
async def test_scan_condition_met_volume_class_lights_up() -> None:
    """量类：anchor.metric=volume + op/level → 窗口 max 达标 → 点亮（无需 close/pct_chg）。"""
    record = _anchor_condition_record(
        {"metric": "volume", "op": "gte", "level": 1.5e8, "threshold": "+3%"},
        "板块放量至 1.5 亿手以上",
    )
    rows = [
        {"trade_date": "2026-09-14", "pct_chg": None, "close": None, "vol": 1.0e8},
        {"trade_date": "2026-09-15", "pct_chg": None, "close": None, "vol": 2.0e8},
    ]
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out["c0"]["condition_met"] is True
    assert out["c0"]["condition_index"] == 0
    assert "result" not in out["c0"]


@pytest.mark.asyncio
async def test_scan_condition_met_volume_class_not_met_on_small_volume() -> None:
    """量类：窗口 max 未达 level → 不产 entry（只写 true）。"""
    record = _anchor_condition_record(
        {"metric": "volume", "op": "gte", "level": 5.0e8}, "板块放量至 5 亿手以上")
    rows = [
        {"trade_date": "2026-09-14", "pct_chg": None, "close": None, "vol": 1.0e8},
        {"trade_date": "2026-09-15", "pct_chg": None, "close": None, "vol": 2.0e8},
    ]
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        assert await pv._scan_condition_met(record) == {}


@pytest.mark.asyncio
async def test_scan_condition_met_ref_level_degrades_to_none() -> None:
    """参考位：metric=today_high 当前取数层不可得（日 K 无 open/high/low）→ 不点亮。"""
    record = _anchor_condition_record(
        {"metric": "today_high", "op": "above"}, "站上今日高点")
    rows = _scan_rows([100.0, 101.0, 102.0])
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock(return_value=rows)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        assert await pv._scan_condition_met(record) == {}


def _event_condition_record(event_ref: str = "EVT-1", record_id: int = 1) -> dict:
    """事件类条件记录：anchor 带 event_ref（类型推断 → 事件类，走状态锚）。"""
    record = _pending_condition_record(
        record_id=record_id, due="2026-09-30", direction="bearish",
        condition="若出口限制细则落地")
    record["created_at"] = "2026-09-10T09:00:00+08:00"
    record["prediction"]["conditions"][0]["anchor"]["event_ref"] = event_ref
    return record


@pytest.mark.asyncio
async def test_scan_condition_met_event_status_anchor_lights_up() -> None:
    """事件三层①（状态锚）：event_status=ongoing/occurred → 确定性点亮，且不拉行情。"""
    for status in ("ongoing", "occurred"):
        record = _event_condition_record()
        entities = [{"event_id": "EVT-1", "event_status": status, "title": "出口限制细则"}]
        with (
            patch.object(pv.node_api, "get_event_entities",
                         new=AsyncMock(return_value=entities)) as events,
            patch.object(pv.node_api, "get_index_kline", new=AsyncMock()) as kline,
            patch("aistock_agent.services.prediction_validator.shanghai_today",
                  return_value=date(2026, 9, 16)),
        ):
            out = await pv._scan_condition_met(record)
        assert out["c0"]["condition_met"] is True
        kline.assert_not_awaited()  # 事件类不需要行情
        events.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_condition_met_event_status_not_yet_returns_empty() -> None:
    """事件未落地（scheduled/upcoming）→ 不点亮（不得按"尚未发生"误判为成立）。"""
    for status in ("scheduled", "upcoming"):
        record = _event_condition_record()
        entities = [{"event_id": "EVT-1", "event_status": status}]
        with (
            patch.object(pv.node_api, "get_event_entities",
                         new=AsyncMock(return_value=entities)),
            patch("aistock_agent.services.prediction_validator.shanghai_today",
                  return_value=date(2026, 9, 16)),
        ):
            assert await pv._scan_condition_met(record) == {}


@pytest.mark.asyncio
async def test_scan_condition_met_event_missing_or_read_failure_returns_empty() -> None:
    """事件三层③（兜底）：event_ref 无对应事件 / 读接口失败 → None（不点亮）。
    ②层（受限 LLM）默认开关关闭（config.condition_met_event_llm_enabled=False）。"""
    record = _event_condition_record(event_ref="EVT-NOT-FOUND")
    with (
        patch.object(pv.node_api, "get_event_entities",
                     new=AsyncMock(return_value=[{"event_id": "EVT-OTHER",
                                                   "event_status": "occurred"}])),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        assert await pv._scan_condition_met(record) == {}
    with (
        patch.object(pv.node_api, "get_event_entities", new=AsyncMock(return_value=None)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        assert await pv._scan_condition_met(record) == {}


@pytest.mark.asyncio
async def test_scan_condition_met_event_lit_without_market_source() -> None:
    """事件类条件不依赖行情：目标资产解析失败（无行情数据源）仍按状态锚点亮。

    回归守卫：早期实现把"无数据源"提前 return，会让可判定的事件条件被无关的行情解析失败连带跳过。
    """
    record = _event_condition_record()
    record["prediction"]["horizons"][0]["target"] = "某不存在的板块名"
    entities = [{"event_id": "EVT-1", "event_status": "occurred"}]
    with (
        patch.object(prediction_validator, "resolve_sector_target",
                     new=AsyncMock(return_value=None)),
        patch.object(pv.node_api, "get_event_entities", new=AsyncMock(return_value=entities)),
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out["c0"]["condition_met"] is True
    # classify_target 按"板块"标记归类（与是否有数据源无关）
    assert out["c0"]["target_type"] == "sector"


# ============ 终审 #3/#5：第①段扫描窗口 = [created_at, today]（上限 120 自然日） ============


@pytest.mark.asyncio
async def test_scan_condition_met_far_due_uses_created_at_window() -> None:
    """#3：due 在 60 天后（旧 [due-20, due+10] 窗口过滤后对今天为空 → 静默跳过、长档
    几乎永不点亮）→ 第①段改用 [created_at, today] 窗口，远端 due 也能点亮；
    且请求区间只由 created_at/today 决定，不依赖 due。"""
    record = _pending_condition_record(due="2026-11-15", direction="bearish")
    record["created_at"] = "2026-09-01T10:20:30.000Z"
    rows = _scan_rows([130.0 - i for i in range(25)])  # 末日 106 < MA20 115.5
    with (
        patch.object(pv.node_api, "get_index_kline",
                     new=AsyncMock(return_value=rows)) as kline,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out["c0"]["condition_met"] is True
    _, kwargs = kline.call_args
    assert kwargs["start_date"] == "20260901"   # created_at（不是 due-20 = 20261026）
    assert kwargs["end_date"] == "20260916"     # today（不是 due+10 = 20261125）


@pytest.mark.asyncio
async def test_scan_condition_met_empty_window_skips_without_request() -> None:
    """#3/#5：窗口为空（created_at 为未来脏值 → 裁剪后 start > end）→ 不产 entry，
    且**不发起请求**（避免必然空请求）。"""
    record = _pending_condition_record(due="2026-11-15", direction="bearish")
    record["created_at"] = "2026-10-01"  # 晚于 today → 空窗
    with (
        patch.object(pv.node_api, "get_index_kline", new=AsyncMock()) as kline,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out == {}
    kline.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_condition_met_missing_created_at_falls_back_120d() -> None:
    """#3：created_at 缺失 → 回退 today-120 自然日，不报错且仍能点亮。"""
    record = _pending_condition_record(due="2026-11-15", direction="bearish")  # 无 created_at
    rows = _scan_rows([130.0 - i for i in range(25)])
    with (
        patch.object(pv.node_api, "get_index_kline",
                     new=AsyncMock(return_value=rows)) as kline,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        out = await pv._scan_condition_met(record)
    assert out["c0"]["condition_met"] is True
    _, kwargs = kline.call_args
    assert kwargs["start_date"] == "20260519"   # 2026-09-16 - 120 自然日
    assert kwargs["end_date"] == "20260916"


@pytest.mark.asyncio
async def test_scan_condition_met_clamps_window_to_120_days() -> None:
    """#3/#5：created_at 早于 today-120 自然日 → 越界裁剪到 120 日上限（不超额拉取）。"""
    record = _pending_condition_record(due="2026-11-15", direction="bearish")
    record["created_at"] = "2026-01-01T00:00:00+08:00"  # 远超 120 天
    rows = _scan_rows([130.0 - i for i in range(25)])
    with (
        patch.object(pv.node_api, "get_index_kline",
                     new=AsyncMock(return_value=rows)) as kline,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        await pv._scan_condition_met(record)
    _, kwargs = kline.call_args
    assert kwargs["start_date"] == "20260519"   # today - 120d（裁剪后）
    assert kwargs["end_date"] == "20260916"


def _two_condition_record(record_id: int, created_at: str, due: str = "2026-09-30") -> dict:
    """同一记录两条 condition（共用 anchor 档位 → 同一扫描窗口）。"""
    record = _pending_condition_record(record_id=record_id, due=due, direction="bearish")
    record["created_at"] = created_at
    record["prediction"]["conditions"].append({
        "condition": "若跌破前低",
        "scenario": "后续 1-4 周下行",
        "anchor": {"horizon": "short", "direction": "bearish", "threshold": ""},
    })
    return record


@pytest.mark.asyncio
async def test_run_once_memoizes_condition_scan_fetch_per_window() -> None:
    """成本（终审附带）：同一 run_once 内 stage① 取数记忆化——同记录多条 condition 只取
    一次数；不同 created_at（窗口不同）不串用缓存（不得用他记录的窗口结果判定）。"""
    rec1 = _two_condition_record(1, "2026-09-01")
    rec2 = _pending_condition_record(record_id=2, due="2026-09-30", direction="bearish")
    rec2["created_at"] = "2026-09-10"
    rows = _scan_rows([130.0 - i for i in range(25)])
    with (
        patch.object(prediction_validator.node_api, "list_pending_predictions",
                     new=AsyncMock(return_value=[rec1, rec2])),
        patch.object(prediction_validator.node_api, "get_index_kline",
                     new=AsyncMock(return_value=rows)) as kline,
        patch.object(prediction_validator.node_api, "update_prediction_verification",
                     new=AsyncMock(return_value={"id": 1})) as update,
        patch("aistock_agent.services.prediction_validator.shanghai_today",
              return_value=date(2026, 9, 16)),
    ):
        updated = await run_once()
    assert updated == 3            # rec1 两条 + rec2 一条，全部点亮
    assert kline.await_count == 2  # rec1 共享一次；rec2 窗口不同 → 各一次
    keys = {(c.args[0], c.args[1]) for c in update.call_args_list}
    assert keys == {(1, "c0"), (1, "c1"), (2, "c0")}
