"""锁定 _build_rhythm_card 的 high/low 缺失真实路径守卫（Fix 1）。

回归：Task 2 的 build_technical_branches 守卫会过滤 None 并在无有效值时返回空
branches + data_missing 留痕；本测试锁定"缺失行直传 None（而非 0.0）"不会被
引擎误当有效点位，从而不伪造支撑/压力。
"""
import pytest

from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card
from aistock_agent.schemas.rhythm_master import MasterRhythmCard, RhythmEvidence


def _win():
    return type("W", (), {"events": [], "high_events": [], "source_missing": False})()


def _card():
    evidence = RhythmEvidence(stage="rally")
    return MasterRhythmCard(
        basis_date="2026-09-04", target_date="2026-09-05",
        refresh_slot="after_close", evidence=evidence,
    )


def _rows(n: int, *, high, low):
    return [
        {"close": 3000.0, "amount": 100.0, "high": high, "low": low}
        for _ in range(n)
    ]


def test_build_rhythm_card_missing_high_low_returns_empty_and_trace():
    """high/low 全缺失：branches 为空，data_missing 含技术支撑/压力留痕。"""
    card = _build_rhythm_card(_card(), _win(), _rows(60, high=None, low=None))
    assert card["branches"] == []
    assert any("技术支撑/压力" in m for m in card["data_missing"])


def test_build_rhythm_card_normal_high_low_returns_branches():
    """high/low 有效：branches 非空（引擎正常生成技术点位）。"""
    card = _build_rhythm_card(_card(), _win(), _rows(60, high=3010.0, low=2990.0))
    assert isinstance(card["branches"], list)
    assert card["branches"]


def test_build_rhythm_card_includes_basis_data_date():
    rows = _rows(60, high=3010.0, low=2990.0)
    for r in rows:
        r["trade_date"] = "20260909"
    card = _build_rhythm_card(_card(), _win(), rows)
    assert card["basis_data_date"] == "20260909"


def test_build_rhythm_card_position_band_has_no_min_max():
    card = _build_rhythm_card(_card(), _win(), _rows(60, high=3010.0, low=2990.0))
    assert set(card["position_band"].keys()) == {"text"}
    assert card["conflict"] is False


def test_build_technical_branches_zero_amounts_falls_back_to_index_point():
    """amounts 全 0 → 成交额三档不可用，退化为指数点位三档并留痕（不产"放量（>0亿）"伪分支）。"""
    from aistock_agent.services.rhythm_engine import build_technical_branches

    missing: list[str] = []
    branches = build_technical_branches(
        closes=[3000.0 + i for i in range(30)],
        highs=[3100.0] * 30,
        lows=[2900.0] * 30,
        amounts=[0.0] * 30,
        data_missing=missing,
    )
    assert branches
    assert all(b["condition"]["indicator"] == "上证指数点位" for b in branches)
    assert any("成交额数据不可用" in m for m in missing)


def test_dead_fields_are_empty_and_documented_without_polluting_gaps():
    """已知空置字段：显式空 + 代码文档标注，但**不写入**对用户可见的缺失清单。

    两个字段的数据源均已接入（温度序列←sentiment 归档、事件窗口←事件日历），
    缺口在「字段未接线」（S4/S5）；该属架构说明，混进 data_missing 会让健康卡
    常驻一条无关提示，也与「降级信息不污染证据清单」的口径冲突。
    """
    from aistock_agent.agents.workers import rhythm_master as wm
    from aistock_agent.schemas.rhythm_master import MasterRhythmCard, RhythmEvidence

    card = MasterRhythmCard(
        basis_date="2026-09-11", target_date="2026-09-14", refresh_slot="after_close",
        evidence=RhythmEvidence(stage="ice", certainty="low"), synthesis=None,
        synthesis_available=False,
    )
    win = type("W", (), {"events": [], "source_missing": False})()
    out = wm._build_rhythm_card(card, win, [])
    # 未接线的两个字段：显式空（不得静默渲染假数据）
    assert out["temperature_series"] == []
    assert out["event_window"] == []
    # 但不写进缺失清单：健康日不得因这两个字段出现常驻提示（G4 精神）
    assert not any("温度序列" in m or "事件窗口" in m for m in out["data_missing"])
    # 「已知空置」仍保留在代码文档中（G7）
    assert "temperature_series" in (wm._build_rhythm_card.__doc__ or "")


def test_next_event_anchor_counts_from_target_date_not_basis_date():
    """锚点「距今天数」以**目标交易日**为原点（该卡描述的那一天）。

    `basis_date` 自 2026-09-14 起表示证据日（K 线末日）；若仍用它作原点，
    盘前 / 午间档（证据日 = 上一交易日）的展示值会相对当天偏大。本用例锁定原点。
    """
    rows = _rows(60, high=3010.0, low=2990.0)
    for r in rows:
        r["trade_date"] = "20260911"  # 证据日（K 线末日）
    win = _win()
    win.events = [
        {"date": "2026-09-16", "title": "FOMC 议息", "importance": "high", "source": "L3"}
    ]
    card = MasterRhythmCard(
        basis_date="2026-09-11", target_date="2026-09-14",
        refresh_slot="morning", evidence=RhythmEvidence(stage="ebb"),
    )
    out = _build_rhythm_card(card, win, rows)
    anchor = out["next_event_anchor"]
    assert anchor is not None
    # 09-16 − 目标日 09-14 = 2 天；若误用证据日 09-11 则为 5 天
    assert anchor["days_until"] == 2
    assert anchor["note"] == "2 天后"


def _card_at(target_date: str, stage: str | None = "ebb") -> MasterRhythmCard:
    return MasterRhythmCard(
        basis_date="2026-09-16", target_date=target_date, refresh_slot="after_close",
        evidence=RhythmEvidence(stage=stage),  # type: ignore[arg-type]
    )


def test_card_event_window_and_hint_open_to_medium() -> None:
    """§5.1/§5.2：交割日（medium）必须进入 event_window 并可产出提示。"""
    win = _win()
    win.events = [{"date": "2026-09-18", "type": "delivery",
                   "title": "2026-09 股指期货交割日", "importance": "medium",
                   "source": "L1"}]
    out = _build_rhythm_card(_card_at("2026-09-17"), win, _rows(60, high=3010.0, low=2990.0))
    assert out["next_event_anchor"] is not None
    assert out["next_event_anchor"]["importance"] == "medium"
    assert out["next_event_anchor"]["note"] == "明日"
    assert out["event_window"] == [{"date": "2026-09-18", "type": "delivery",
                                    "title": "2026-09 股指期货交割日",
                                    "importance": "medium"}]
    assert "不改仓位倾向" in out["event_high_hint"]


def test_medium_event_does_not_change_position_text() -> None:
    """验收 3：只被看见不改数值——仅中级事件时仓位文案与"无事件"基线逐字相同。"""
    rows = _rows(60, high=3010.0, low=2990.0)
    baseline = _build_rhythm_card(_card_at("2026-09-17"), _win(), rows)
    win = _win()
    win.events = [{"date": "2026-09-18", "type": "delivery",
                   "title": "2026-09 股指期货交割日", "importance": "medium",
                   "source": "L1"}]
    out = _build_rhythm_card(_card_at("2026-09-17"), win, rows)
    assert out["position_band"]["text"] == baseline["position_band"]["text"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
