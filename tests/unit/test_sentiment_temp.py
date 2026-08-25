"""短线情绪温度计算纯函数测试。"""
import pytest

from aistock_agent.services.sentiment_temp import (
    _indicator_scores,
    compute_sentiment_score,
    judge_ice,
    sentiment_level,
)


def test_compute_sentiment_score_hot_market() -> None:
    # 涨停 90、跌停 2、炸板 5、最高连板 8、涨跌家数比 0.8、主力净流入 120 亿
    a_share: dict[str, object] = {
        "limits": {"up_count": 90, "down_count": 2, "broken_count": 5, "highest_board": 8},
        "breadth": {"advance_ratio": 0.8},
        "main_force": {"large_and_extra_large_net_yuan": 120.0e8},
    }
    score = compute_sentiment_score(a_share)
    # 涨停→100 跌停→90 炸板≈51→90? 见断点：broken/(90+5)=0.053<0.15→90
    # 连板 8→100 涨跌比 0.8→90 主力 120e8→65
    # 加权=100*.25+90*.25+90*.15+100*.15+90*.15+65*.05=92.75
    assert score == pytest.approx(92.75, abs=0.06)


def test_compute_sentiment_score_ice_market() -> None:
    # up 需 < 10 才能落入 10 分档：up=10 时涨停分档仍 20，温度 22.2 不达冰点；
    # up=5 → broken_ratio=40/45≈0.89 仍 ≥0.6，温度 19.8 < 20 判冰点
    a_share: dict[str, object] = {
        "limits": {"up_count": 5, "down_count": 96, "broken_count": 40, "highest_board": 2},
        "breadth": {"advance_ratio": 0.21},
        "main_force": {"large_and_extra_large_net_yuan": -128.5e8},
    }
    assert compute_sentiment_score(a_share) < 20


def test_indicator_scores_missing_fields_neutral() -> None:
    a_share: dict[str, object] = {"breadth": {"advance_ratio": 0.6}}
    scores = _indicator_scores(a_share)
    # 缺失的 limits/main_force → 中性 50
    assert scores["up_count"] == 50
    assert scores["main_force"] == 50
    assert scores["advance_ratio"] == pytest.approx(70, abs=1e-6)


def test_indicator_scores_broken_ratio_zero_up_guard() -> None:
    a_share: dict[str, object] = {
        "limits": {"up_count": 0, "down_count": 0, "broken_count": 0, "highest_board": 0},
        "breadth": {"advance_ratio": 0.5},
        "main_force": {"large_and_extra_large_net_yuan": 0.0},
    }
    scores = _indicator_scores(a_share)
    # up+broken==0 → 炸板率中性 0.5 → 分段映射 40，不 ZeroDivision
    assert scores["broken_ratio"] == 40


def test_sentiment_level_borders() -> None:
    assert sentiment_level(20) == "冰点"
    assert sentiment_level(45) == "低迷"
    assert sentiment_level(55) == "常温"
    assert sentiment_level(80) == "活跃"
    assert sentiment_level(81) == "亢奋"


@pytest.mark.parametrize(
    ("score", "prev", "is_ice", "consecutive", "extreme"),
    [
        (18, 0, True, 1, False),
        (18, 1, True, 2, True),
        (50, 1, False, 0, False),
    ],
)
def test_judge_ice(
    score: float, prev: int, is_ice: bool, consecutive: int, extreme: bool
) -> None:
    result = judge_ice(score, prev, threshold=20, extreme_days=2)
    assert result["is_ice"] is is_ice
    assert result["consecutive_ice_days"] == consecutive
    assert result["is_extreme_ice"] is extreme
