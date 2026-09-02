"""sector_tools 测试"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult
from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.sector_tools import (
    _format_wind_leaders,
    get_leader_stocks,
    get_wind_leaders,
    predict_sector_trend,
)


@pytest.mark.asyncio
async def test_get_leader_stocks_success():
    """get_leader_stocks 正常返回龙头股"""
    mock_data = {
        "tag_name": "白酒",
        "leaders": [
            {"name": "贵州茅台", "code": "600519", "change_pct": 2.5, "reason": "业绩超预期"},
            {"name": "五粮液", "code": "000858", "change_pct": 1.8, "reason": "北向资金流入"},
        ],
    }
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_leader_stocks.ainvoke({"tag_code": "BK0475"})
        assert "白酒" in result
        assert "贵州茅台" in result
        mock_api.get.assert_called_once_with("/internal/leader/BK0475")


@pytest.mark.asyncio
async def test_get_leader_stocks_empty():
    """get_leader_stocks 空数据"""
    mock_data = {"tag_name": "白酒", "leaders": []}
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_leader_stocks.ainvoke({"tag_code": "BK0475"})
        assert "暂无龙头股" in result


# ── get_wind_leaders ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_wind_leaders_success():
    """get_wind_leaders 正常返回风口龙头板块及龙头股"""
    mock_data = {
        "update_time": "2026-07-08 09:30",
        "hot_sectors": [
            {
                "name": "半导体",
                "today_change": 3.2,
                "leading_stock": "中芯国际",
                "leading_change": 8.5,
                "main_stocks": [
                    {
                        "code": "688981",
                        "name": "中芯国际",
                        "change_pct": 8.5,
                        "reason": "国产替代加速",
                    },
                ],
            },
        ],
    }
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_wind_leaders.ainvoke({})
        assert "半导体" in result
        assert "中芯国际" in result
        mock_api.get.assert_called_once_with("/internal/wind-leaders")


@pytest.mark.asyncio
async def test_get_wind_leaders_degradation():
    """get_wind_leaders 异常时返回降级文本"""
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_wind_leaders.ainvoke({})
        assert result == DEGRADED_MESSAGE


# ── predict_sector_trend（Spec D · 预判环 · 对话补充入口）────────────


def _trend_prediction() -> PredictionResult:
    return PredictionResult(
        schema_version="3.0",
        prediction_status="hypothesis",
        horizons=[
            PredictionHorizon(
                horizon="short",
                remaining_estimate="1-3日",
                phase="peaking",
                direction="bearish",
                target="存储板块",
                metric_projection="相对现价区间波动",
                confidence="medium",
            )
        ],
        evolution_narrative="短线弱势震荡后回稳",
        risks=[],
        evidence_ids=[],
    )


@pytest.mark.asyncio
async def test_predict_sector_trend_success_renders_markdown():
    """板块预判成功 → 渲染影响持续性预判文本（统一走 predict_sector 入口）"""
    with patch(
        "aistock_agent.services.prediction_service.predict_sector",
        AsyncMock(return_value=_trend_prediction()),
    ) as mock_predict:
        result = await predict_sector_trend.ainvoke({"sector_name": "存储板块"})
    assert "影响持续性预判" in result
    assert "短线(1-5交易日)" in result
    assert "方向：bearish" in result
    mock_predict.assert_awaited_once()
    assert mock_predict.await_args.kwargs["sector_name"] == "存储板块"


@pytest.mark.asyncio
async def test_predict_sector_trend_none_returns_hint():
    """板块解析失败/未产出 → 降级提示（不抛错）"""
    with patch(
        "aistock_agent.services.prediction_service.predict_sector",
        AsyncMock(return_value=None),
    ):
        result = await predict_sector_trend.ainvoke({"sector_name": "存储板块"})
    assert "暂无法预判" in result


@pytest.mark.asyncio
async def test_predict_sector_trend_blank_name():
    """空板块名 → 提示缺板块名，不触发 predict_sector"""
    with patch(
        "aistock_agent.services.prediction_service.predict_sector",
        AsyncMock(return_value=None),
    ) as mock_predict:
        result = await predict_sector_trend.ainvoke({"sector_name": "  "})
    assert "缺少板块名称" in result
    mock_predict.assert_not_awaited()


# ── _format_wind_leaders（短长线分类标注）──────────────────────────


def test_format_wind_leaders_cycle_label():
    """验证 _format_wind_leaders 双链分节，缺省 cycle 兜底短线"""
    data = {
        "update_time": "2026-08-04 09:00",
        "hot_sectors": [
            {
                "name": "人工智能",
                "cycle": "long",
                "today_change": 3.2,
                "leading_stock": "科大讯飞",
                "ai_analysis": {
                    "long_term_days": 45,
                    "long_confidence": 0.8,
                    "long_reason": "政策加码",
                    "short_term_days": 0,
                    "short_heat": 0,
                    "short_reason": "",
                },
            },
            {
                "name": "白酒",
                "cycle": "short",
                "today_change": 1.1,
                "leading_stock": "贵州茅台",
                "ai_analysis": {
                    "long_term_days": 0,
                    "long_confidence": 0,
                    "long_reason": "",
                    "short_term_days": 3,
                    "short_heat": 0.5,
                    "short_reason": "换手过热",
                },
            },
            {"name": "无cycle字段板块", "today_change": 0.5, "leading_stock": "-"},
        ],
    }
    text = _format_wind_leaders(data)
    assert "【长线链研判】" in text
    assert "【短线链研判】" in text
    assert "人工智能" in text.split("【短线链研判】")[0]       # long 只在长线节
    assert "白酒" not in text.split("【短线链研判】")[0]       # short 不在长线节
    assert "无cycle字段板块" in text.split("【短线链研判】")[1]  # 缺省兜底 short 进短线节


def test_format_wind_leaders_dual_chain_brief():
    """双链简报：长线节/短线节/both 归属/缺省"""
    data = {
        "update_time": "2026-08-05 09:30",
        "hot_sectors": [
            {
                "name": "半导体",
                "cycle": "long",
                "today_change": 2.1,
                "leading_stock": "中芯国际",
                "ai_analysis": {
                    "long_term_days": 45,
                    "long_confidence": 0.8,
                    "logic_type": "政策",
                    "long_reason": "中央政策加码，月线多头",
                    "heat_stage": "发酵期",
                    "short_term_days": 5,
                    "short_heat": 0.6,
                    "short_reason": "连板高度支撑热度",
                },
            },
            {
                "name": "光伏",
                "cycle": "both",
                "today_change": 1.5,
                "leading_stock": "隆基绿能",
                "ai_analysis": {
                    "long_term_days": 40,
                    "long_confidence": 0.7,
                    "logic_type": "政策",
                    "long_reason": "政策+业绩双支撑",
                    "heat_stage": "启动期",
                    "short_term_days": 8,
                    "short_heat": 0.7,
                    "short_reason": "首次放量启动",
                },
            },
            {
                "name": "白酒",
                "cycle": "short",
                "today_change": 0.8,
                "leading_stock": "贵州茅台",
                "ai_analysis": {
                    "long_term_days": 0,
                    "long_confidence": 0,
                    "logic_type": "无支撑",
                    "long_reason": "",
                    "heat_stage": "高潮期",
                    "short_term_days": 3,
                    "short_heat": 0.5,
                    "short_reason": "换手过热见顶风险",
                },
            },
            {"name": "旧数据", "today_change": 0.5, "leading_stock": "--"},
        ],
    }
    result = _format_wind_leaders(data)
    assert "【长线链研判】" in result
    assert "【短线链研判】" in result
    assert "半导体" in result.split("【短线链研判】")[0]      # 半导体(仅long)只在长线节
    assert "光伏" in result.split("【长线链研判】")[1]       # both 进长线节
    assert "光伏" in result.split("【短线链研判】")[1]       # both 也进短线节
    assert "白酒" in result.split("【短线链研判】")[1]       # 白酒(short)只在短线节
    assert "中央政策加码" in result and "首次放量启动" in result  # 理由字段透出
    assert "旧数据" in result.split("【短线链研判】")[1]      # 缺省按 short 进短线节
