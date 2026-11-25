from aistock_agent.services.event_scoring import apply_rule_score, apply_source_score


def test_peripheral_never_positive():
    raw = {"title": "美联储加息预期升温", "content": "", "summary": ""}
    apply_rule_score(raw, source="cls", origin="peripheral")
    assert raw["direction"] == "neutral"          # 不再被 STRONG_POSITIVE("加息") 抬成 positive


def test_global_markets_direction_neutral():
    raw = {"title": "恒生指数 隔夜表现", "content": "-2.4%", "summary": "", "impact_score": 5}
    apply_source_score(raw, source="global_markets", origin="peripheral")
    assert raw["direction"] == "neutral" and raw["impact_score"] == 5


def test_domestic_keeps_legacy_behavior():
    raw = {"title": "某公司中标重大项目", "content": "", "summary": ""}
    apply_rule_score(raw, source="cls")
    assert raw["direction"] == "positive"
