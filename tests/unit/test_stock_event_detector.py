"""个股事件检测器单元测试 — 第二阶段：STOCK/UNKNOWN 二值判定。

规则覆盖：
1. eastmoney_rule：source=eastmoney 且存在 symbol → STOCK（个股情报管线产物）。
2. company_event_rule：股票名称实体命中（stock_basic_index 索引） + 企业行为词命中 → STOCK。
3. 其余一律 UNKNOWN（宁可漏判个股事件，不误伤行业/市场级事件）。

symbol 只表示事件关联股票，不能单独触发 STOCK。
"""

import pytest

import aistock_agent.services.stock_basic_index as sbi
from aistock_agent.services.stock_event_detector import detect_stock_event


@pytest.fixture(autouse=True)
def _reset_stock_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例前重置股票名称索引（模块级全局状态，避免用例间污染/误触真实接口）。"""
    monkeypatch.setattr(sbi, "_names_longest_first", ())
    monkeypatch.setattr(sbi, "_loaded", False)
    monkeypatch.setattr(sbi, "_load_task", None)
    yield


def _inject_names(names: list[str]) -> None:
    sbi.inject_names(names)


def test_eastmoney_source_with_symbol_is_stock() -> None:
    result = detect_stock_event(
        "贵州茅台拟回购股份", "", {"symbol": "600519"}, "eastmoney"
    )
    assert result["event_scope"] == "STOCK"
    assert result["event_scope_source"] == "eastmoney_rule"
    assert result["event_scope_confidence"] == 0.95


def test_company_strong_action_event_is_stock() -> None:
    _inject_names(["贵州茅台"])
    result = detect_stock_event("贵州茅台回购股份", "", {}, "cls")
    assert result["event_scope"] == "STOCK"
    assert result["event_scope_source"] == "company_event_rule"


def test_company_weak_action_event_is_stock() -> None:
    _inject_names(["宁德时代"])
    result = detect_stock_event("宁德时代获得海外订单", "", {}, "cls")
    assert result["event_scope"] == "STOCK"
    assert result["event_scope_source"] == "company_event_rule"


def test_company_news_without_action_word_is_unknown() -> None:
    # 命中股票名称但没有企业行为词 → 不能直接判 STOCK
    _inject_names(["宁德时代"])
    result = detect_stock_event("宁德时代推动新能源汽车发展", "", {}, "cls")
    assert result["event_scope"] == "UNKNOWN"


def test_national_policy_news_is_unknown() -> None:
    result = detect_stock_event("国家支持新能源汽车产业发展", "", {}, "cls")
    assert result["event_scope"] == "UNKNOWN"


def test_macro_news_is_unknown() -> None:
    result = detect_stock_event("美联储降息", "", {}, "global_markets")
    assert result["event_scope"] == "UNKNOWN"


def test_industry_news_with_symbol_is_not_stock() -> None:
    # symbol 只表示关联，行业/产业链新闻可能携带关联股票 symbol，不能单独判 STOCK
    result = detect_stock_event(
        "新能源产业链发展趋势", "", {"symbol": "300750"}, "cls"
    )
    assert result["event_scope"] == "UNKNOWN"


def test_empty_name_index_makes_company_rule_inert() -> None:
    # 股票基础库未加载（索引为空）：行为词命中也不判 STOCK，不误伤行业/市场事件
    result = detect_stock_event("贵州茅台回购股份", "", {}, "cls")
    assert result["event_scope"] == "UNKNOWN"


def test_eastmoney_without_symbol_falls_through() -> None:
    # 防御：eastmoney 但无 symbol（理论不出现）→ 不命中 eastmoney_rule
    result = detect_stock_event("贵州茅台回购股份", "", {}, "eastmoney")
    assert result["event_scope"] == "UNKNOWN"
