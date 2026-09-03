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


def test_evaluate_branch_uses_anchor_threshold() -> None:
    branch = {
        "condition": {
            "kind": "interval", "indicator": "上证指数点位",
            "lo": 4000.0, "hi": None, "label": "站上 4000",
        },
        "position_action": {"direction": "add", "change": "+2 成", "band": None},
        "anchor": {"metric": "index_close", "threshold": "站稳 +0.5%", "direction": "bullish"},
        "conclusion": {
            "direction": "bullish", "range": "4000-4030",
            "validity": 5, "note": "突破压力位",
        },
        "touch_strength": 0.12,
    }
    rows = [
        {"close": 4020.0},
        {"close": 4030.0},
    ]
    result = evaluate_branch(branch, rows, {})
    # 站上 4000 且窗口内 2 日 > 触发位 → 命中（anchor direction bullish，阈值站稳）
    assert result in ("hit", "miss", "insufficient")
    # anchor 机械判定：窗口内连续 2 日 close > 触发位 lo=4000 → hit
    assert result == "hit"


def test_evaluate_branch_anchor_bullish_miss_when_not_stable() -> None:
    """anchor bull：以 condition.lo 为触发位，需连续 2 日 close > 触发位才算 hit。

    窗口内仅首日站上（4010>4000），后续回落 → anchor 判 miss；
    旧 range 判定（3990-4010 包含 3990）会误判 hit → 本用例 RED。
    """
    branch = {
        "condition": {
            "kind": "interval", "indicator": "上证指数点位",
            "lo": 4000.0, "hi": None, "label": "站上 4000",
        },
        "position_action": {"direction": "add", "change": "+2 成", "band": None},
        "anchor": {"metric": "index_close", "threshold": "站稳 +0.5%", "direction": "bullish"},
        "conclusion": {
            "direction": "bullish", "range": "3990-4010",
            "validity": 5, "note": "突破压力位",
        },
        "touch_strength": 0.12,
    }
    rows = [
        {"close": 4010.0},
        {"close": 3990.0},
        {"close": 3990.0},
        {"close": 3990.0},
        {"close": 3990.0},
    ]
    assert evaluate_branch(branch, rows, {}) == "miss"


def test_evaluate_branch_anchor_bearish_uses_hi_trigger() -> None:
    """anchor bear：跌破支撑以 condition.hi 为触发位（非 lo），连续 2 日 close < hi → hit。

    旧 range 判定只覆盖窄区间（3998-4000）→ miss；anchor bear 正确判 hit → 本用例 RED。
    """
    branch = {
        "condition": {
            "kind": "interval", "indicator": "上证指数点位",
            "lo": None, "hi": 4000.0, "label": "跌破 4000",
        },
        "position_action": {"direction": "reduce", "change": "-1 成", "band": None},
        "anchor": {"metric": "index_close", "threshold": "跌破 -0.5%", "direction": "bearish"},
        "conclusion": {
            "direction": "bearish", "range": "3998-4000",
            "validity": 5, "note": "跌破支撑位",
        },
        "touch_strength": 0.2,
    }
    rows = [
        {"close": 3990.0},
        {"close": 3985.0},
    ]
    assert evaluate_branch(branch, rows, {}) == "hit"


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
