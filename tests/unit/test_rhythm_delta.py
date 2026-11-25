import pytest

from aistock_agent.services.rhythm_delta import build_delta, resolve_prev


def test_resolve_prev_after_close_uses_basis() -> None:
    assert resolve_prev(target_date="2026-11-06", slot="after_close", basis_date="2026-11-05") == (
        "2026-11-05", "after_close", False,
    )


def test_resolve_prev_morning_uses_prev_close_card() -> None:
    assert resolve_prev(target_date="2026-11-06", slot="morning", basis_date="2026-11-05") == (
        "2026-11-06", "after_close", False,
    )


def test_resolve_prev_midday_uses_morning() -> None:
    assert resolve_prev(target_date="2026-11-06", slot="midday", basis_date="2026-11-05") == (
        "2026-11-06", "morning", True,
    )


PREV_CONTENT = {
    "target_date": "2026-11-05",
    "basis_date": "2026-11-04",
    "refresh_slot": "after_close",
    "rhythm_card": {
        "position_index": 1,
        "position_band": {"text": "建议仓位：轻仓~三成"},
        "phase": "ebb",
        "mainline_snapshot": {"name": "AI 应用", "excess": 0.23, "strength": "weak", "state": "established"},
        "position_ledger": {
            "items": [
                {"key": "trend", "status": "blocking", "value_text": "", "judgement": "", "effect": ""},
                {"key": "mainline", "status": "blocking", "value_text": "", "judgement": "", "effect": ""},
            ],
            "watch_points": [
                {"key": "gate", "gap": {"key": "gate", "current": 3680.0, "target": 3701.53, "gap": 21.53, "unit": "点", "direction": "up"}},
            ],
        },
    },
}


def _current_card(*, ledger_items: list[dict] | None = None):
    from aistock_agent.schemas.rhythm_master import (
        GapValue,
        LedgerItem,
        MasterRhythmCard,
        PositionBand,
        PositionLedger,
        RhythmEvidence,
        WatchPoint,
    )

    ledger = PositionLedger(
        items=[LedgerItem(**i) for i in (ledger_items or [])],
        base_index=0, final_index=0, gate_hit=True, gate_blocked=True,
        mainline_state="none",
        watch_points=[
            WatchPoint(
                key="gate", condition="c", current_text="t", sufficient_alone=False,
                gap=GapValue(key="gate", current=3689.91, target=3701.53, gap=11.62, unit="点", direction="up"),
            )
        ],
    )
    evidence = RhythmEvidence(
        stage="overheat",
        position=PositionBand(text="建议仓位：空仓观望", action="hold", direction="neutral", index=0),
        position_ledger=ledger,
    )
    return MasterRhythmCard(
        basis_date="2026-11-05", target_date="2026-11-06",
        refresh_slot="after_close", evidence=evidence,
    )


def test_build_delta_dimensions() -> None:
    d = build_delta(
        prev_content=PREV_CONTENT, card=_current_card(),
        mainline_facts={"name": "液冷服务器", "excess": -1.13, "strength": "none", "state": "none"},
    )
    assert d is not None
    assert d.position.from_index == 1 and d.position.to_index == 0 and d.position.changed is True
    assert d.stage == {"from": "ebb", "to": "overheat"}
    # 当前卡 items=[] → 上一条的两个阻塞项都解除了
    assert d.blocking.resolved == ["trend", "mainline"]
    assert d.blocking.added == [] and d.blocking.persisting == []
    assert d.gaps[0].key == "gate"
    assert d.gaps[0].from_gap == pytest.approx(21.53) and d.gaps[0].to_gap == pytest.approx(11.62)
    assert d.mainline is not None
    assert d.mainline.from_name == "AI 应用" and d.mainline.to_name == "液冷服务器"
    assert d.mainline.strength_changed is True
    # 阻塞项有解除 → 取该 key（trend 优先映射到 gate）
    assert d.default_key == "gate"


def test_build_delta_blocking_persisting() -> None:
    """持久项：上一条与当前都卡着 trend；mainline 由卡着变不卡 → resolved。"""
    d = build_delta(
        prev_content=PREV_CONTENT,
        card=_current_card(ledger_items=[
            {"key": "trend", "status": "blocking", "value_text": "", "judgement": "", "effect": ""},
            {"key": "mainline", "status": "neutral", "value_text": "", "judgement": "", "effect": ""},
        ]),
        mainline_facts=None,
    )
    assert d is not None
    assert d.blocking.persisting == ["trend"]
    assert d.blocking.resolved == ["mainline"]
    assert d.blocking.added == []
    assert d.default_key == "mainline"


def test_build_delta_without_prev_returns_none() -> None:
    assert build_delta(prev_content=None, card=_current_card(), mainline_facts=None) is None


def test_build_delta_old_card_without_ledger_degrades() -> None:
    """旧卡（无 position_ledger）：只比仓位/stage，其余置空（spec §9）。"""
    old = {
        "target_date": "2026-11-05",
        "rhythm_card": {"position_index": 1, "position_band": {"text": "建议仓位：轻仓~三成"}, "phase": "ebb"},
    }
    d = build_delta(prev_content=old, card=_current_card(), mainline_facts=None)
    assert d is not None
    assert d.position.from_index == 1 and d.position.to_index == 0
    assert d.gaps == []
    assert d.blocking.resolved == [] and d.blocking.added == [] and d.blocking.persisting == []
    assert d.mainline is None
    assert d.stage == {"from": "ebb", "to": "overheat"}


def test_default_key_prefers_largest_relative_gap() -> None:
    """无阻塞项变化时，取 gap 相对变化最大者（分母下限 1.0，防爆比值）。"""
    prev = {
        "target_date": "2026-11-05", "basis_date": "2026-11-04", "refresh_slot": "after_close",
        "rhythm_card": {
            "position_index": 0, "phase": "ebb",
            "position_ledger": {"items": [], "watch_points": [
                {"key": "gate", "gap": {"key": "gate", "current": 3600.0, "target": 3701.53, "gap": 101.53, "unit": "点", "direction": "up"}},
                {"key": "mainline", "gap": {"key": "mainline", "current": 0.0, "target": 2.0, "gap": 2.0, "unit": "pct", "direction": "up"}},
            ]},
        },
    }
    from aistock_agent.schemas.rhythm_master import (
        GapValue,
        MasterRhythmCard,
        PositionBand,
        PositionLedger,
        RhythmEvidence,
        WatchPoint,
    )

    ledger = PositionLedger(items=[], base_index=0, final_index=0, watch_points=[
        WatchPoint(key="gate", condition="c", current_text="t", sufficient_alone=False,
                   gap=GapValue(key="gate", current=3689.91, target=3701.53, gap=11.62, unit="点", direction="up")),
        WatchPoint(key="mainline", condition="c", current_text="t", sufficient_alone=False,
                   gap=GapValue(key="mainline", current=1.0, target=2.0, gap=1.0, unit="pct", direction="up")),
    ])
    card = MasterRhythmCard(
        basis_date="2026-11-05", target_date="2026-11-06", refresh_slot="after_close",
        evidence=RhythmEvidence(stage="ebb",
            position=PositionBand(text="建议仓位：空仓观望", action="hold", direction="neutral", index=0),
            position_ledger=ledger),
    )
    d = build_delta(prev_content=prev, card=card, mainline_facts=None)
    assert d is not None
    # gate: |11.62-101.53|/101.53 ≈ 0.885；mainline: |1.0-2.0|/2.0 = 0.5 → gate 更大
    assert d.default_key == "gate"
