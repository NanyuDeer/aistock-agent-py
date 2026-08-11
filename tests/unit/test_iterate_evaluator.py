"""evaluator —— 归因相似度三档评分"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.evaluator import evaluate_attribution, extract_agent_attribution

GT = {
    "gt_id": "gt_20260731_us_market_surge",
    "confidence": "high",
    "attribution": {
        "direction": "bullish",
        "drivers": ["隔夜美股暴涨", "外盘传导"],
        "transmission_path": ["美股 → A股高开"],
        "affected_sectors": ["半导体", "算力", "新能源"],
        "source_notes": [],
    },
}

AGENT_OUT = "大盘高开 1.2%，主因为隔夜美股大涨带动风险偏好回升。半导体板块领涨 3.2%。"


def _mock_llm_extract(direction: str, drivers: list[str], sectors: list[str]) -> object:
    payload = {"direction": direction, "drivers": drivers, "sectors": sectors}
    return type("R", (), {"content": json.dumps(payload)})()


def _mock_driver_judge(hit: int, total: int) -> object:
    payload = {"hit_count": hit, "total_count": total}
    return type("R", (), {"content": json.dumps(payload)})()


@pytest.mark.asyncio
async def test_perfect_match_scores_high() -> None:
    """evaluate_attribution 内部调两次 LLM：extract（提取）→ judge（要素命中）。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract(
                    "bullish", ["隔夜美股暴涨", "外盘传导"], ["半导体", "算力", "新能源"]
                ),
                _mock_driver_judge(hit=2, total=2),
            ]
        )
        score = await evaluate_attribution(AGENT_OUT, GT)
    assert score.total >= 0.8
    assert score.direction == 0.2
    assert score.drivers == 0.5
    assert score.sectors == 0.3


@pytest.mark.asyncio
async def test_wrong_direction_loses_direction_score() -> None:
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                _mock_llm_extract("bearish", ["国内政策收紧"], ["银行"]),
                _mock_driver_judge(hit=0, total=2),
            ]
        )
        score = await evaluate_attribution(AGENT_OUT, GT)
    assert score.direction == 0.0
    assert score.sectors == 0.0
    assert score.total < 0.5


@pytest.mark.asyncio
async def test_sector_overlap_partial() -> None:
    """agent drivers 为空时 _driver_hit_score 直接返回 0.0，不调 judge LLM。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("bullish", [], ["半导体", "银行", "白酒"])
        )
        score = await evaluate_attribution(AGENT_OUT, GT)
    # 板块重叠 1/3 ≈ 0.1
    assert 0.05 <= score.sectors <= 0.15


@pytest.mark.asyncio
async def test_extract_agent_attribution_returns_struct() -> None:
    """提取函数返回结构必须包含三键（供评分使用）。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("bullish", ["a"], ["半导体"])
        )
        parsed = await extract_agent_attribution("大盘高开 1.2%")
    assert {"direction", "drivers", "sectors"} <= set(parsed)
