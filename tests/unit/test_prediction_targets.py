# tests/unit/test_prediction_targets.py
from aistock_agent.services.prediction_targets import INDEX_TARGETS, classify_target


def test_index_targets_cover_common_indexes():
    assert INDEX_TARGETS["上证指数"] == "000001"
    assert INDEX_TARGETS["沪深300"] == "000300"
    assert len(INDEX_TARGETS) >= 8


def test_classify_target_index():
    assert classify_target("上证指数") == "index"


def test_classify_target_sector():
    # D4：板块词（板块/概念/行业）→ sector（板块源 P1-5 未接，reason 区分）
    assert classify_target("半导体板块") == "sector"
    assert classify_target("白酒概念") == "sector"


def test_classify_target_stock():
    # D4：6 位数字代码 → stock（个股源未接，reason 区分）
    assert classify_target("600519") == "stock"
    assert classify_target("000001") == "stock"


def test_classify_target_unknown():
    # G6：LLM 自由文本抽象词（无板块/个股特征）→ unknown（target 漂移信号）
    assert classify_target("市场") == "unknown"
    assert classify_target("情绪") == "unknown"
