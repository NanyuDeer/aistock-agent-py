"""分支验证（§19.4/G14）：窗口 5 交易日/触及/站稳 hit-miss/未触发 insufficient；
事件落档后判 hit/miss（D11）。

Task 7（方案丙 min 边界）新增：run_once 读取语义改为 morning/midday 优先、
after_close 兜底；after_close 存储 report_date=target_date 不改，本任务只对齐
当天精确命中语义（Node 无"最新卡"端点，见 Task 8 开放项）。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.rhythm_verification import (
    evaluate_branch,
    hit_rate_summary,
    run_once,
)


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


def test_amount_branch_with_anchor_falls_to_range_miss() -> None:
    """成交额分支 + anchor 不能拿"指数点位 close"去比"亿元 cond.lo"：应回退 range 判点。

    anchor bullish + 成交额 lo=1.5（亿元），若误走点位机械判定：close(点位 3000)>1.5 → 恒 hit；
    修复后回退 conclusion.range（点位区间 3050-3070），close 未进区间 → miss。
    """
    branch = {
        "condition": {
            "kind": "interval", "indicator": "成交额",
            "lo": 1.5, "hi": None, "unit": "亿元", "label": "放量",
        },
        "anchor": {"metric": "amount", "threshold": "放量", "direction": "bullish"},
        "conclusion": {
            "direction": "bullish", "range": "3050.00-3070.00",
            "validity": 5, "note": "",
        },
    }
    rows = _rows([3000.0, 3010.0, 3005.0, 3008.0, 3003.0])
    assert evaluate_branch(branch, rows, {}) == "miss"


def test_amount_branch_with_anchor_hit_via_range() -> None:
    """成交额分支 + anchor：仍可经 range 判 hit（回退路径），而非点位机械判定。"""
    branch = {
        "condition": {
            "kind": "interval", "indicator": "成交额",
            "lo": 1.5, "hi": None, "unit": "亿元", "label": "放量",
        },
        "anchor": {"metric": "amount", "threshold": "放量", "direction": "bullish"},
        "conclusion": {
            "direction": "bullish", "range": "3020.00-3040.00",
            "validity": 5, "note": "",
        },
    }
    rows = _rows([3000.0, 3030.0, 3035.0, 3010.0, 3005.0])
    assert evaluate_branch(branch, rows, {}) == "hit"


def test_neutral_anchor_falls_to_range() -> None:
    """neutral anchor 无点位机械触发位，应回退 range 判点（hit），而非点位机械判定。"""
    branch = {
        "condition": {
            "kind": "interval", "indicator": "上证指数点位",
            "lo": None, "hi": None, "label": "区间震荡",
        },
        "anchor": {"metric": "index_close", "threshold": "区间", "direction": "neutral"},
        "conclusion": {
            "direction": "neutral", "range": "3020.00-3040.00",
            "validity": 5, "note": "",
        },
    }
    rows = _rows([3000.0, 3030.0, 3035.0, 3010.0, 3005.0])
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


@pytest.mark.asyncio
async def test_run_once_uses_latest_after_close_when_target_misses() -> None:
    """Task 7/min 边界：morning/midday 当天无 rhythm_card 命中时，after_close 兜底命中。

    方案丙语义：after_close 存储 report_date=target_date 不改，run_once 不要求"当天
    精确匹配 after_close"；测试 mock 须 async-correct（get_rhythm_report 等被 await，
    须用 AsyncMock，plain MagicMock+同步 lambda 会 TypeError）。
    """
    from aistock_agent.utils.date import shanghai_today

    target = shanghai_today().isoformat()
    basis_card = {
        "content": {"basis_date": "2026-09-04", "rhythm_card": {"branches": []}},
        "report_date": target,
    }
    with (
        patch(
            "aistock_agent.services.rhythm_verification.add_trading_days",
            side_effect=lambda d, n: d,
        ),
        patch("aistock_agent.services.rhythm_verification.node_api") as api,
    ):
        api.get_index_kline = AsyncMock(
            return_value=[{"trade_date": target, "close": 3000.0, "pct_chg": 0.0}]
        )
        api.get_rhythm_report = AsyncMock(
            side_effect=lambda d, slot: basis_card if slot == "after_close" else None
        )
        api.get_calendar_events = AsyncMock(return_value=[])
        result = await run_once(target)
    # 方案丙：即使当天 target 精确读命中，也应读到最新 after_close 卡继续，不作"基准报告缺失"
    assert result.get("error") != "基准报告缺失"
