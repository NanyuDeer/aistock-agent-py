"""midday 板块筛选器测试（deterministic opportunity/risk selection）。"""

from aistock_agent.services.midday_sectors import (
    classify_regime,
    select_opportunities,
    select_risks,
)


def _sectors(
    gainers: list[dict] | None = None,
    losers: list[dict] | None = None,
    breadth: dict | None = None,
    indexes: list[dict] | None = None,
) -> dict:
    return {
        "indexes": indexes or [{"code": "000001", "pct_chg": 0.3}],
        "breadth": breadth or {"advance_ratio": 0.7, "avg_change_pct": 0.5},
        "gainers": gainers or [
            {"name": "半导体", "pct_change": 3.2, "net_amount": 1e5, "lead_stock": "中芯"}
        ],
        "losers": losers or [
            {"name": "光伏设备", "pct_change": -2.1, "net_amount": -8e4, "lead_stock": "隆基"}
        ],
        "availability": {"state": "available"},
    }


def test_classify_regime_strong_when_breadth_positive():
    assert classify_regime({"advance_ratio": 0.7, "avg_change_pct": 0.5}, []) == "strong"


def test_classify_regime_weak_when_breadth_negative():
    assert classify_regime({"advance_ratio": 0.3, "avg_change_pct": -0.6}, []) == "weak"


def test_classify_regime_weak_when_missing_breadth():
    assert classify_regime(None, []) == "weak"


def test_select_opportunities_filters_by_strength_and_excess():
    s = _sectors(
        gainers=[
            {"name": "半导体", "pct_change": 3.2, "net_amount": 1e5, "lead_stock": "中芯"},
            {"name": "光伏", "pct_change": 1.2, "net_amount": 1e5, "lead_stock": "隆基"},
        ],
        indexes=[{"code": "000001", "pct_chg": 0.3}],
    )
    # 半导体超额 2.9pp 合格；光伏 1.2<2.0 被剔除
    assert select_opportunities(s) == ["半导体"]


def test_select_opportunities_empty_when_weak_regime():
    s = _sectors(
        gainers=[{"name": "半导体", "pct_change": 3.2, "net_amount": 1e5, "lead_stock": "中芯"}],
        breadth={"advance_ratio": 0.3, "avg_change_pct": -0.6},
    )
    assert select_opportunities(s) == []


def test_select_opportunities_truncates_to_5_and_8chars():
    # 7 个全部达标（pct_change>=2.0 且超额>=1.5pp），应截断为 5
    gainers = [
        {"name": f"板块{i}", "pct_change": 6.0 - i * 0.4, "net_amount": 1e5, "lead_stock": "X"}
        for i in range(7)
    ]
    s = _sectors(gainers=gainers, indexes=[{"code": "000001", "pct_chg": 0.0}])
    opp = select_opportunities(s)
    assert len(opp) == 5
    assert all(len(x) <= 8 for x in opp)


def test_select_risks_disjoint_and_short():
    s = _sectors(
        gainers=[{"name": "半导体", "pct_change": 3.2, "net_amount": 1e5, "lead_stock": "中芯"}],
        losers=[
            {"name": "光伏设备", "pct_change": -2.1, "net_amount": -8e4, "lead_stock": "隆基"},
            {"name": "锂电", "pct_change": -3.0, "net_amount": -6e4, "lead_stock": "宁德"},
        ],
        breadth={"advance_ratio": 0.7, "avg_change_pct": 0.5},
    )
    opp = select_opportunities(s)
    risk = select_risks(s)
    assert risk == ["光伏设备", "锂电"]
    assert all(len(x) <= 8 for x in risk)
    # 集合不相交：机会与风险不得指向同一板块
    assert set(opp) & set(risk) == set()
