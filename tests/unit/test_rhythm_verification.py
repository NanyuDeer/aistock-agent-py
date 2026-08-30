"""分支验证（§19.4/G14）：窗口 5 交易日/触及/站稳 hit-miss/未触发 insufficient；
事件落档后判 hit/miss（D11）。
"""
from aistock_agent.services.rhythm_verification import evaluate_branch, hit_rate_summary


def _rows(closes: list[float]) -> list[dict]:
    return [
        {"trade_date": f"2026-09-0{i+1}", "close": c, "amount": 120.0}
        for i, c in enumerate(closes)
    ]


def test_technical_branch_stay_hit() -> None:
    branch = {
        "condition": {
            "kind": "interval",
            "indicator": "成交额",
            "lo": 100.0,
            "hi": None,
            "unit": "亿元",
            "label": "放量",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "3020.00-3040.00",
            "validity": 5,
            "note": "",
        },
    }
    # 成交额 120 > 100 触发；连续 2 日收盘进入 range → hit
    rows = _rows([3000.0, 3030.0, 3035.0, 3010.0, 3005.0])
    assert evaluate_branch(branch, rows, {}) == "hit"


def test_technical_branch_condition_not_triggered_insufficient() -> None:
    branch = {
        "condition": {
            "kind": "interval",
            "indicator": "成交额",
            "lo": 200.0,
            "hi": None,
            "unit": "亿元",
            "label": "放量",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "3020.00-3040.00",
            "validity": 5,
            "note": "",
        },
    }
    rows = _rows([3000.0] * 5)  # 成交额 120 < 200 未触发
    assert evaluate_branch(branch, rows, {}) == "insufficient"


def test_technical_branch_triggered_but_miss() -> None:
    branch = {
        "condition": {
            "kind": "interval",
            "indicator": "成交额",
            "lo": 100.0,
            "hi": None,
            "unit": "亿元",
            "label": "放量",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "3050.00-3070.00",
            "validity": 5,
            "note": "",
        },
    }
    rows = _rows([3000.0, 3010.0, 3005.0, 3008.0, 3003.0])
    assert evaluate_branch(branch, rows, {}) == "miss"


def test_event_branch_result_triggered_then_range() -> None:
    branch = {
        "condition": {
            "kind": "enum",
            "indicator": "英伟达财报预期差",
            "value": "超预期",
            "label": "超预期",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "3020.00-3040.00",
            "validity": 5,
            "note": "",
        },
        "event_ref": {"event_date": "2026-09-02", "title": "英伟达财报"},
    }
    rows = _rows([3000.0, 3030.0, 3032.0, 3010.0, 3005.0])
    # 事件 result=超预期 → 条件触发；连续 2 日进 range → hit（D11：不再恒 insufficient）
    assert evaluate_branch(branch, rows, {"英伟达财报": "超预期"}) == "hit"
    # 事件未公布 result → insufficient
    assert evaluate_branch(branch, rows, {}) == "insufficient"


def test_event_branch_result_landed_hit_via_range() -> None:
    """事件 result 落档（value=超预期）+ range 非空 → 触发且站稳 → hit（D11）。"""
    branch = {
        "condition": {
            "kind": "enum",
            "indicator": "英伟达财报预期差",
            "value": "超预期",
            "label": "超预期",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "3020.00-3040.00",
            "validity": 5,
            "note": "事件结果已公布：超预期，按预期差落档，目标区间由 engine 按当日行情计算",
        },
        "event_ref": {"event_date": "2026-09-02", "title": "英伟达财报"},
    }
    rows = _rows([3000.0, 3030.0, 3032.0, 3010.0, 3005.0])
    assert evaluate_branch(branch, rows, {"英伟达财报": "超预期"}) == "hit"


def test_event_branch_result_landed_miss_via_range() -> None:
    """事件 result 落档 + range 非空但未站稳 → miss（触发成功而非恒 insufficient，D11）。"""
    branch = {
        "condition": {
            "kind": "enum",
            "indicator": "英伟达财报预期差",
            "value": "超预期",
            "label": "超预期",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "3050.00-3070.00",
            "validity": 5,
            "note": "事件结果已公布：超预期，按预期差落档，目标区间由 engine 按当日行情计算",
        },
        "event_ref": {"event_date": "2026-09-02", "title": "英伟达财报"},
    }
    rows = _rows([3000.0, 3010.0, 3005.0, 3008.0, 3003.0])
    assert evaluate_branch(branch, rows, {"英伟达财报": "超预期"}) == "miss"


def test_event_branch_trigger_prefers_event_ref_title() -> None:
    """_triggered 优先用 event_ref.title 匹配事件 result（title 含"预期差"字样仍能匹配，D11）。"""
    branch = {
        "condition": {
            "kind": "enum",
            "indicator": "CPI预期差预期差",
            "value": "不及预期",
            "label": "不及预期",
        },
        "conclusion": {
            "direction": "bearish",
            "range": "2980.00-3000.00",
            "validity": 5,
            "note": "",
        },
        "event_ref": {"event_date": "2026-09-02", "title": "CPI预期差"},
    }
    rows = _rows([3000.0, 3010.0, 3005.0, 3008.0, 3003.0])
    # event_ref.title="CPI预期差" 命中 result=不及预期 → 触发（未站稳 miss）；
    # 若退化为 indicator 去前缀（"CPI"）则匹配不到 → insufficient
    assert evaluate_branch(branch, rows, {"CPI预期差": "不及预期"}) == "miss"


def test_hit_rate_summary() -> None:
    summary = hit_rate_summary(["hit", "miss", "insufficient", "hit"])
    assert summary["hit"] == 2 and summary["miss"] == 1 and summary["insufficient"] == 1
    assert summary["hit_rate"] == 2 / 3
