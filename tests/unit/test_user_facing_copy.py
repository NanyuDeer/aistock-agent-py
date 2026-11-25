import pytest

from aistock_agent.services.user_facing_copy import check_copy, find_engineering_terms


def test_finds_engineering_term():
    assert find_engineering_terms("base→0，禁止 +1") == ["base"]


def test_case_insensitive():
    assert find_engineering_terms("MA5 回到 MA10 上方") == ["ma5", "ma10"]


def test_allows_stock_domain_words():
    # L1 炒股域词汇永不入黑名单
    assert find_engineering_terms("主线由「AI 应用」换成「液冷服务器」，超额收益 +0.2 个百分点") == []


def test_check_copy_raises_with_terms():
    with pytest.raises(ValueError, match="闸门"):
        check_copy("两条闸门同时压到底")


def test_check_copy_passes_clean_text():
    check_copy("两个条件同时卡着，只解决一个也还不能加仓")
