"""板块溯源多板块消费测试（spec P1a-2）。"""
import pytest

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation


@pytest.mark.parametrize(
    "sector_pct,index_pct,expected",
    [
        (3.0, -0.5, "self_driven"),
        (-2.0, 0.5, "self_driven"),
        (3.0, 1.0, "self_driven"),
        (0.8, 1.0, "market_follow"),
        (None, 1.0, "unknown"),
    ],
)
def test_judge_sector_driver_relation(sector_pct, index_pct, expected):
    assert judge_sector_driver_relation(sector_pct, index_pct) == expected
