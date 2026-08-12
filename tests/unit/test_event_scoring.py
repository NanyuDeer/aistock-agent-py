import pytest

from aistock_agent.services.event_scoring import apply_rule_score


def test_strong_positive_scores_5():
    raw = {"title": "央行宣布降准0.5个百分点", "summary": "央行降准", "url": "https://x"}
    apply_rule_score(raw, source="cls")
    assert raw["impact_score"] == 5
    assert raw["direction"] == "positive"


def test_strong_negative_scores_5():
    raw = {"title": "XX公司遭证监会立案调查", "url": "https://x"}
    apply_rule_score(raw, source="cls")
    assert raw["impact_score"] == 5
    assert raw["direction"] == "negative"


def test_weak_positive_scores_3():
    raw = {"title": "公司业绩小幅增长", "url": "https://x"}
    apply_rule_score(raw, source="ths_original")
    assert raw["impact_score"] == 3
    assert raw["direction"] == "positive"


def test_neutral_context_downgrades_to_1():
    raw = {"title": "重大节假日休市安排", "url": "https://x"}
    apply_rule_score(raw, source="cls")
    assert raw["impact_score"] == 1
    assert raw["direction"] == "neutral"


def test_no_keyword_scores_1():
    raw = {"title": "今日市场收评", "url": "https://x"}
    apply_rule_score(raw, source="tavily")
    assert raw["impact_score"] == 1
    assert raw["direction"] == "neutral"


def test_existing_score_not_overwritten():
    raw = {"title": "某公司重组", "impact_score": 5, "direction": "positive", "url": "https://x"}
    apply_rule_score(raw, source="eastmoney")
    assert raw["impact_score"] == 5
    assert raw["direction"] == "positive"
