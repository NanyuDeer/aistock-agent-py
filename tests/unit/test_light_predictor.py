# tests/unit/test_light_predictor.py
r"""自选股洞察轻量预判任务（阶段 2）单元测试。

Mock 策略：按仓库惯例 patch.object 真实 node_api 实例的方法为 AsyncMock
（light_predictor 与 tests 引用同一 NodeApiClient 实例），
with_chat_structured_output 打桩为固定返回 LightForecast 的 Runnable。
运行：`.venv311\Scripts\python.exe -m pytest tests/unit/test_light_predictor.py -q`
"""
from contextlib import ExitStack, contextmanager
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.prediction import LightForecast, PredictionAnchor, PredictionCondition
from aistock_agent.services.data_client import node_api
from aistock_agent.services.light_predictor import (
    _kline_feature,
    _num,
    build_forecast_payload,
    run_light_prediction,
)

D = date(2026, 9, 3)


def _forecast() -> LightForecast:
    return LightForecast(
        summary="若业绩兑现延续且放量则上看 +5%；若量能萎缩回落则看回 20 日线。",
        conditions=[
            PredictionCondition(
                condition="业绩兑现延续且放量",
                scenario="上看 +5%",
                anchor=PredictionAnchor(
                    horizon="short", threshold="+5%", metric="volume", direction="bullish",
                ),
            ),
            PredictionCondition(
                condition="量能萎缩回落",
                scenario="回踩 20 日线",
                anchor=PredictionAnchor(
                    horizon="mid", threshold="-3%", metric="close", direction="bearish",
                ),
            ),
        ],
    )


class _FakeStructured:
    """模拟 with_chat_structured_output 返回的 Runnable（ainvoke 直接回 payload）。"""

    def __init__(self, payload: LightForecast | Exception) -> None:
        self._payload = payload

    async def ainvoke(self, _messages: object) -> LightForecast:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _target_event() -> dict:
    return {
        "symbol": "600519",
        "stock_name": "贵州茅台",
        "event": {
            "eventId": "mv:600519:2026-09-03:111:up",
            "direction": "up",
            "change_pct": 9.9,
            "severity": "high",
            "is_limit_up": True,
            "analysis_status": "completed",
            "primary_cause": "产品提价业绩预期大增",
        },
        "intel": [
            {"id": 42, "title": "茅台提价公告", "summary": "系列酒提价10%", "impact": "重大利好"},
        ],
    }


def _target_intel_only() -> dict:
    return {
        "symbol": "000858",
        "stock_name": "五粮液",
        "intel": [
            {"id": 7, "title": "五粮液回购方案", "summary": "拟回购5亿元", "impact": "重大利好"},
            {"id": 8, "title": "行业价格战传闻", "summary": "多家酒企让利", "impact": "重大利空"},
        ],
    }


@contextmanager
def _node_mocks(**kwargs: AsyncMock):
    """把 node_api 实例方法替换为 AsyncMock（kwargs: 方法名→AsyncMock）。"""
    ctx_managers = [patch.object(node_api, name, new=mock) for name, mock in kwargs.items()]
    with ExitStack() as stack:
        for cm in ctx_managers:
            stack.enter_context(cm)
        yield


# ── 纯函数：_num / _kline_feature / build_forecast_payload ──


def test_num_parses_and_handles_bad_values():
    assert _num("1.5") == 1.5
    assert _num(None) is None
    assert _num("abc") is None
    assert _num(float("nan")) is None


def test_kline_feature_derives_ma_highlow_volume():
    rows = [
        {
            "trade_date": f"2026-{i + 1:02d}-01",
            "close": 10 + i * 0.1,
            "high": 11 + i * 0.1,
            "low": 9 + i * 0.1,
            "vol": 1000 + i * 10,
        }
        for i in range(30)
    ]
    feat = _kline_feature(rows)
    assert feat["ma5"] is not None and feat["ma10"] is not None and feat["ma20"] is not None
    tail20 = rows[-20:]
    assert feat["high_20d"] == max(r["high"] for r in tail20)
    assert feat["low_20d"] == min(r["low"] for r in tail20)
    assert feat["vol_recent5_avg"] > feat["vol_prev5_avg"]


def test_kline_feature_empty_or_short_rows():
    assert _kline_feature(None) == {}
    assert _kline_feature([]) == {}
    # 单行：无足够历史产出均线/区间/量能 → {}（prompt 缺量能时不产 volume 条件）
    assert _kline_feature([{"trade_date": "d", "close": 10}]) == {}


def test_build_forecast_payload_shape():
    payload = build_forecast_payload("close", "trace", _forecast())
    assert payload["schema_version"] == "1"
    assert payload["scenario"] == "trace"
    assert payload["slot"] == "close"
    assert payload["summary"]
    assert len(payload["conditions"]) == 2
    cond = payload["conditions"][0]
    assert cond["anchor"]["horizon"] == "short"
    assert cond["anchor"]["threshold"] == "+5%"
    assert "generated_at" in payload


# ── 编排：run_light_prediction ──


@pytest.mark.asyncio
@patch("aistock_agent.services.light_predictor.shanghai_today", return_value=D)
async def test_run_returns_zero_when_no_targets(_mock_today: object):
    list_mock = AsyncMock(return_value=[])
    set_event_mock = AsyncMock()
    set_judge_mock = AsyncMock()
    with _node_mocks(
            list_light_predict_targets=list_mock,
            set_event_forecast=set_event_mock,
            set_judgement_forecast=set_judge_mock,
    ):
        written = await run_light_prediction("midday")
    assert written == 0
    set_event_mock.assert_not_awaited()
    set_judge_mock.assert_not_awaited()


@pytest.mark.asyncio
@patch("aistock_agent.services.light_predictor.shanghai_today", return_value=D)
@patch("aistock_agent.services.light_predictor.with_chat_structured_output")
async def test_event_target_writes_event_forecast(mock_structured: object, _mock_today: object):
    fake = _FakeStructured(_forecast())
    mock_structured.return_value = fake
    event_mock = AsyncMock(return_value={"event_id": "mv:...", "slot": "close"})
    judge_mock = AsyncMock()
    with _node_mocks(
            list_light_predict_targets=AsyncMock(return_value=[_target_event()]),
            get_quote=AsyncMock(
                return_value={"最新价": 1520.0, "涨跌幅": 9.9},
            ),
            get_stock_kline=AsyncMock(return_value=[
                {"trade_date": "2026-09-02", "close": 1380, "high": 1390, "low": 1370, "vol": 500},
                {"trade_date": "2026-09-03", "close": 1520, "high": 1520, "low": 1380, "vol": 800},
            ]),
            set_event_forecast=event_mock,
            set_judgement_forecast=judge_mock,
    ):
        written = await run_light_prediction("close")

    assert written == 1
    event_id, slot, payload = event_mock.await_args.args
    assert event_id == "mv:600519:2026-09-03:111:up"
    assert slot == "close"
    assert payload["scenario"] == "trace"
    judge_mock.assert_not_awaited()


@pytest.mark.asyncio
@patch("aistock_agent.services.light_predictor.shanghai_today", return_value=D)
@patch("aistock_agent.services.light_predictor.with_chat_structured_output")
async def test_intel_only_target_writes_judgement_forecast(
    mock_structured: object, _mock_today: object,
):
    fake = _FakeStructured(_forecast())
    mock_structured.return_value = fake
    judge_mock = AsyncMock(return_value={"id": 7, "slot": "midday"})
    event_mock = AsyncMock()
    with _node_mocks(
            list_light_predict_targets=AsyncMock(return_value=[_target_intel_only()]),
            get_quote=AsyncMock(return_value=None),
            get_stock_kline=AsyncMock(return_value=None),
            set_judgement_forecast=judge_mock,
            set_event_forecast=event_mock,
    ):
        written = await run_light_prediction("midday")

    assert written == 1
    judgement_id, slot, payload = judge_mock.await_args.args
    # 写入当日该股最新一条重大资讯（intel 按 published_at DESC，第一条即最新）
    assert judgement_id == 7
    assert slot == "midday"
    assert payload["scenario"] == "intel"
    event_mock.assert_not_awaited()


@pytest.mark.asyncio
@patch("aistock_agent.services.light_predictor.shanghai_today", return_value=D)
@patch("aistock_agent.services.light_predictor.with_chat_structured_output")
async def test_llm_failure_skips_target(mock_structured: object, _mock_today: object):
    fake = _FakeStructured(RuntimeError("llm boom"))
    mock_structured.return_value = fake
    event_mock = AsyncMock()
    with _node_mocks(
            list_light_predict_targets=AsyncMock(return_value=[_target_event()]),
            get_quote=AsyncMock(return_value=None),
            get_stock_kline=AsyncMock(return_value=None),
            set_event_forecast=event_mock,
    ):
        written = await run_light_prediction("close")

    assert written == 0
    event_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_slot_raises():
    with _node_mocks(list_light_predict_targets=AsyncMock(return_value=[])):
        with pytest.raises(ValueError):
            await run_light_prediction("noon")


@pytest.mark.asyncio
@patch("aistock_agent.services.light_predictor.shanghai_today", return_value=D)
async def test_no_targets_skips_llm(_mock_today: object):
    """空候选不触发 LLM（token 控制：范围严格限定并集子集）。"""
    with _node_mocks(list_light_predict_targets=AsyncMock(return_value=[])):
        written = await run_light_prediction("close")
    assert written == 0
