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


"""评分重归一化 + direction_present（A12/A15/A3 修复）"""


@pytest.mark.asyncio
async def test_empty_ground_truth_scores_zero_not_full() -> None:
    """空 GT（direction=neutral + sectors=[] + drivers=[]）不得满分：total=0.0。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": [],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        # extract 返回空结构；drivers 空 → 不调 judge
        factory.return_value.ainvoke = AsyncMock(
            return_value=type(
                "R",
                (),
                {
                    "content": json.dumps(
                        {"direction": "neutral", "drivers": [], "sectors": []}
                    )
                },
            )()
        )
        score = await evaluate_attribution("任何输出", gt)
    assert score.total == 0.0
    assert score.available_weight == 0.0  # 三维全部无对比对象


@pytest.mark.asyncio
async def test_neutral_direction_excluded_from_denominator() -> None:
    """GT direction=neutral 时方向维不参与分母（direction_present=False），
    板块+驱动全中仍可达 1.0（重归一化），但方向错误不再贡献 0.2 白给。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": ["外盘传导"],
            "affected_sectors": ["半导体"],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            side_effect=[
                # extract：direction=neutral（撞 GT）、drivers/sectors 全中
                type(
                    "R",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "direction": "neutral",
                                "drivers": ["外盘传导"],
                                "sectors": ["半导体"],
                            }
                        )
                    },
                )(),
                # judge：hit=1 total=1
                type("R", (), {"content": json.dumps({"hit_count": 1, "total_count": 1})})(),
            ]
        )
        score = await evaluate_attribution("外盘传导，半导体领涨", gt)
    # 可用权重 = drivers 0.5 + sectors 0.3 = 0.8；得分 = 0.5+0.3=0.8 → total=1.0
    assert score.total == 1.0
    assert score.available_weight == 0.8


"""gap_analysis 感知 present 维度（Task 6 审查 Important 修复）"""


@pytest.mark.asyncio
async def test_gap_analysis_no_phantom_gap_when_all_dims_excluded() -> None:
    """GT direction=neutral + sectors=[] + drivers=[]：三维全部被排除出评分，
    gap_analysis 不得含假缺口——应为"无显著差距"（而非必报"方向不一致"等）。"""
    gt = {
        "attribution": {
            "direction": "neutral",
            "drivers": [],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        # extract 返回 neutral（与 GT 语义匹配）；drivers 空 → 不调 judge
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("neutral", [], [])
        )
        score = await evaluate_attribution("任何输出", gt)
    assert "方向不一致" not in score.gap_analysis
    assert "板块覆盖不足" not in score.gap_analysis
    assert "驱动要素覆盖不足" not in score.gap_analysis
    assert score.gap_analysis == "无显著差距"


@pytest.mark.asyncio
async def test_gap_analysis_reports_direction_only_when_present() -> None:
    """GT direction=bullish + sectors=[] + drivers=[]：仅方向维参与评分，
    方向不匹配应报"方向不一致"；被排除的板块/驱动两维不得报假缺口。"""
    gt = {
        "attribution": {
            "direction": "bullish",
            "drivers": [],
            "affected_sectors": [],
        }
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=_mock_llm_extract("bearish", ["国内政策收紧"], ["银行"])
        )
        score = await evaluate_attribution("偏空解读", gt)
    assert "方向不一致" in score.gap_analysis
    assert "板块覆盖不足" not in score.gap_analysis
    assert "驱动要素覆盖不足" not in score.gap_analysis
