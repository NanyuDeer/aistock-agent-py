"""晨报结构化提取服务测试。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.market_trace import MorningForecast
from aistock_agent.services.morning_forecast_extractor import (
    _enrich_morning_summary_with_profile,
    extract_morning_forecast,
)


@pytest.fixture(autouse=True)
def _isolate_profile_read():
    """隔离展示侧反哺依赖：默认样本不足→不追加，保证现有断言稳定、不碰真实数据源。"""
    with patch(
        "aistock_agent.skills.prediction_validation.read_validation_profile",
        new_callable=AsyncMock,
        return_value={"sufficient_sample": False, "n": 0, "hit_rate": 0.0},
    ), patch(
        "aistock_agent.services.target_profile.make_target",
        return_value=object(),
    ):
        yield


def _make_forecast(summary: str = "原文摘要") -> MorningForecast:
    return MorningForecast(
        report_date="2026-08-02",
        summary=summary,
        major_events=[],
        sectors=[],
        risks=[],
        source_report_id=None,
    )


@pytest.mark.asyncio
async def test_extract_morning_forecast_success():
    """成功场景：morning 报告存在，LLM 提取结构化预测。"""
    mock_report = {
        "id": "rpt_001",
        "content": {
            "display_report": {
                "summary": "A股有望震荡上行",
                "details": "今日关注：美联储维持利率利好券商板块；新能源汽车补贴延续利好锂电；地缘风险需关注。",
                "stocks": [],
                "risks": ["外部地缘风险", "美联储政策不确定"],
            },
            "schema_version": "2.0",
        },
    }
    mock_llm_response = """
    {
      "report_date": "2026-08-02",
      "summary": "A股有望震荡上行",
      "major_events": [
        {"title": "美联储维持利率", "direction": "bullish", "affected_sectors": ["券商"]}
      ],
      "sectors": [
        {"sector": "券商", "direction": "bullish", "note": "政策利好"},
        {"sector": "锂电", "direction": "bullish", "note": "补贴延续"}
      ],
      "risks": ["外部地缘风险", "美联储政策不确定"],
      "source_report_id": "rpt_001"
    }
    """

    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=mock_report,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.set_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_quick_think",
    ) as mock_llm:
        mock_llm.return_value.ainvoke = AsyncMock(return_value=type("M", (), {"content": mock_llm_response})())
        result = await extract_morning_forecast("2026-08-02")

    assert result is not None
    assert isinstance(result, MorningForecast)
    assert result.report_date == "2026-08-02"
    assert result.summary == "A股有望震荡上行"
    assert len(result.major_events) == 1
    assert result.major_events[0].title == "美联储维持利率"
    assert len(result.sectors) == 2
    assert result.sectors[0].sector == "券商"
    assert result.source_report_id == "rpt_001"


@pytest.mark.asyncio
async def test_extract_morning_forecast_report_missing():
    """morning 报告不存在时返回 None。"""
    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await extract_morning_forecast("2026-08-02")
    assert result is None


@pytest.mark.asyncio
async def test_extract_morning_forecast_cache_hit():
    """缓存命中时直接返回，不调 LLM。"""
    cached = {
        "report_date": "2026-08-02",
        "summary": "缓存命中",
        "major_events": [],
        "sectors": [],
        "risks": [],
        "source_report_id": None,
    }
    with patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=cached,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
    ) as mock_fetch:
        result = await extract_morning_forecast("2026-08-02")
    assert result is not None
    assert result.summary == "缓存命中"
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_extract_morning_forecast_llm_failure():
    """LLM 调用失败时返回 None，不抛异常。"""
    mock_report = {
        "id": "rpt_001",
        "content": {
            "display_report": {"summary": "x", "details": "x", "stocks": [], "risks": []},
            "schema_version": "2.0",
        },
    }
    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=mock_report,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_quick_think",
        side_effect=RuntimeError("LLM 不可用"),
    ):
        result = await extract_morning_forecast("2026-08-02")
    assert result is None


@pytest.mark.asyncio
async def test_enrich_morning_summary_with_profile_adds_hit_rate():
    """sufficient_sample 时在 summary 追加历史命中率，其它字段保持不变。"""
    forecast = _make_forecast()
    with patch(
        "aistock_agent.skills.prediction_validation.read_validation_profile",
        new_callable=AsyncMock,
        return_value={"sufficient_sample": True, "n": 40, "hit_rate": 0.75},
    ):
        result = await _enrich_morning_summary_with_profile(forecast)
    assert result.summary == "原文摘要；历史大盘预判命中率 75%（n=40）"
    assert result.report_date == forecast.report_date
    assert result.major_events == []
    assert result.source_report_id == forecast.source_report_id


@pytest.mark.asyncio
async def test_enrich_morning_summary_with_profile_insufficient_sample():
    """样本不足时不追加，保持原文。"""
    forecast = _make_forecast()
    result = await _enrich_morning_summary_with_profile(forecast)
    assert result.summary == "原文摘要"


@pytest.mark.asyncio
async def test_enrich_morning_summary_with_profile_read_failure():
    """读取失败时降级返回原 forecast，不阻断晨报产出。"""
    forecast = _make_forecast()
    with patch(
        "aistock_agent.skills.prediction_validation.read_validation_profile",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = await _enrich_morning_summary_with_profile(forecast)
    assert result.summary == "原文摘要"


@pytest.mark.asyncio
async def test_enrich_morning_summary_with_profile_no_target():
    """target 解析失败时返回原 forecast。"""
    forecast = _make_forecast()
    with patch("aistock_agent.services.target_profile.make_target", return_value=None):
        result = await _enrich_morning_summary_with_profile(forecast)
    assert result.summary == "原文摘要"


@pytest.mark.asyncio
async def test_extract_morning_forecast_enriches_summary():
    """end-to-end：LLM 提取后展示侧反哺追加历史命中率到 summary。"""
    mock_report = {
        "id": "rpt_001",
        "content": {
            "display_report": {
                "summary": "A股有望震荡上行",
                "details": "今日关注：政策利好。",
                "stocks": [],
                "risks": [],
            },
            "schema_version": "2.0",
        },
    }
    mock_llm_response = """
    {
      "report_date": "2026-08-02",
      "summary": "A股有望震荡上行",
      "major_events": [],
      "sectors": [],
      "risks": [],
      "source_report_id": "rpt_001"
    }
    """
    with patch(
        "aistock_agent.services.morning_forecast_extractor.node_api.get_analysis_report",
        new_callable=AsyncMock,
        return_value=mock_report,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.set_cached_morning_forecast",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "aistock_agent.services.morning_forecast_extractor.get_quick_think",
    ) as mock_llm, patch(
        "aistock_agent.skills.prediction_validation.read_validation_profile",
        new_callable=AsyncMock,
        return_value={"sufficient_sample": True, "n": 40, "hit_rate": 0.75},
    ):
        mock_llm.return_value.ainvoke = AsyncMock(
            return_value=type("M", (), {"content": mock_llm_response})()
        )
        result = await extract_morning_forecast("2026-08-02")

    assert result is not None
    assert result.summary == "A股有望震荡上行；历史大盘预判命中率 75%（n=40）"
