"""midday 盘中修正 + 盘中因子落库（职责重定义 spec §4.3 / §4.4）。

覆盖：
- 规则 A/B/C 的命中 / 不命中 / 组合（只降不升，单次最多 2 档，A 直达 0）；
- 数据可用性（`availability.state` / `intr_close <= 0`）一律不修正；
- 盘中因子恒落库（不可用时字段存在且为 null，键结构与 §4.4 一致）；
- 归档优先、缺失回退实时取数；
- 临时 bar 估算 ma20 的数值口径；
- worker 的 midday 分支：降档时账本 / 观察点 / position_source 同步。
"""

import json
from contextlib import ExitStack
from datetime import date as date_cls
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import rhythm_master as wm
from aistock_agent.schemas.rhythm_master import LedgerItem, PositionLedger
from aistock_agent.services import rhythm_engine as engine
from aistock_agent.services.rhythm_intraday import (
    IntradayFactors,
    apply_intraday_rules,
    derive_intraday_factors,
    intraday_available,
    is_intraday_usable,
)

TARGET = "2026-11-04"
BASIS_DATE = "2026-11-03"

# 均线空头排列（4000 → 3520，步长 -20）：规则 A 的必要条件
FALLING = [4000.0 - i * 20.0 for i in range(25)]
# 均线多头 / 震荡向上（3000 → 3240，步长 +10）：规则 A 不触发
RISING = [3000.0 + i * 10.0 for i in range(25)]

FACTOR_KEYS = {
    "captured_at", "intr_pct", "intr_ratio", "intr_close", "intr_ma20",
    "mainline_pct", "threshold_version", "triggered_rules",
}


def _snapshot(
    *,
    close: float = 3250.0,
    pct: float = 0.4,
    ratio: float = 0.60,
    state: str = "available",
    gainers: list[dict[str, object]] | None = None,
    losers: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """`get_intraday_sectors()` 原始返回形态。"""
    return {
        "captured_at": "2026-11-04T04:25:00.000Z",
        "indexes": [
            {"code": "000300", "name": "沪深300", "pct_chg": 0.1, "close": 4000.0},
            {"code": "000001", "name": "上证指数", "close": close, "pct_chg": pct},
        ],
        "breadth": {"advance_ratio": ratio, "avg_change_pct": 0.0},
        "gainers": gainers or [],
        "losers": losers or [],
        "availability": {"state": state},
    }


def _archive(
    *, close: float | None, pct: float | None, ratio: float | None,
    raw: dict[str, object] | None = None,
) -> dict[str, object]:
    """T0 归档 payload 形态（扁平字段 + raw）。"""
    return {
        "date": TARGET,
        "captured_at": "2026-11-04T04:25:00+08:00",
        "intr_close": close,
        "intr_pct": pct,
        "intr_ratio": ratio,
        "raw": raw,
    }


def _factors(
    closes: list[float], snapshot: dict[str, object] | None, mainline_name: str | None = None
) -> IntradayFactors:
    return derive_intraday_factors(snapshot, closes, mainline_name=mainline_name)


# ============ 1-6. 规则判定（纯函数） ============


def test_rule_a_breaks_to_zero_from_any_base() -> None:
    """规则 A：盘中破位直接压到 0 档，base=4 也直达 0。"""
    snap = _snapshot(close=3200.0, pct=-1.5, ratio=0.60)
    factors = _factors(FALLING, snap)
    assert factors.intr_close == 3200.0
    assert factors.intr_ma5 is not None and factors.intr_ma10 is not None
    assert factors.intr_ma5 < factors.intr_ma10 < factors.intr_ma20  # 空头排列
    for base in (0, 1, 2, 3, 4):
        new_index, rules = apply_intraday_rules(base, factors, snap)
        assert new_index == 0
        assert rules == ["A"]


def test_rule_b_breadth_collapse_drops_one() -> None:
    """规则 B：宽度 ≤0.35 且跌幅 ≤-1.0% → 降 1 档。"""
    snap = _snapshot(close=3250.0, pct=-1.2, ratio=0.30)
    factors = _factors(RISING, snap)
    new_index, rules = apply_intraday_rules(3, factors, snap)
    assert rules == ["B"]
    assert new_index == 2


def test_rule_c_mainline_breakdown_drops_one() -> None:
    """规则 C：主线板块当日跌幅 ≤-2.5% → 降 1 档。"""
    snap = _snapshot(
        close=3245.0, pct=0.5, ratio=0.55,
        losers=[{"name": "半导体", "pct_change": -3.2}],
    )
    factors = _factors(RISING, snap, mainline_name="半导体")
    assert factors.mainline_pct == pytest.approx(-3.2)
    new_index, rules = apply_intraday_rules(3, factors, snap)
    assert rules == ["C"]
    assert new_index == 2


def test_all_rules_hit_caps_drop_at_two_and_a_goes_to_zero() -> None:
    """A+B+C 全命中：A 直达 0（最多降 2 档的约束不限制 A）。"""
    snap = _snapshot(
        close=3200.0, pct=-1.5, ratio=0.30,
        losers=[{"name": "半导体", "pct_change": -3.0}],
    )
    factors = _factors(FALLING, snap, mainline_name="半导体")
    for base in (2, 3, 4):
        new_index, rules = apply_intraday_rules(base, factors, snap)
        assert rules == ["A", "B", "C"]
        assert new_index == 0


def test_b_plus_c_without_a_drops_at_most_two() -> None:
    """B+C（无 A）→ 降 2 档，且不低于 0。"""
    snap = _snapshot(
        close=3250.0, pct=-1.2, ratio=0.30,
        losers=[{"name": "半导体", "pct_change": -3.0}],
    )
    factors = _factors(RISING, snap, mainline_name="半导体")
    assert apply_intraday_rules(4, factors, snap) == (2, ["B", "C"])
    assert apply_intraday_rules(1, factors, snap) == (0, ["B", "C"])


def test_no_rule_hit_keeps_index() -> None:
    """未触发 → 档位不变、triggered_rules 为空。"""
    snap = _snapshot(close=3245.0, pct=0.4, ratio=0.60)
    factors = _factors(RISING, snap)
    assert apply_intraday_rules(3, factors, snap) == (3, [])
    assert factors.triggered_rules == []


@pytest.mark.parametrize("base", [0, 1, 2, 3, 4])
def test_morning_rally_never_upgrades(base: int) -> None:
    """回归：早盘大涨（宽度 85%、指数 +2%）绝不升档（任何路径 new <= base）。"""
    snap = _snapshot(close=3260.0, pct=2.0, ratio=0.85)
    factors = _factors(RISING, snap)
    new_index, rules = apply_intraday_rules(base, factors, snap)
    assert rules == []
    assert new_index == base
    assert new_index <= base


# ============ 7-9. 可用性 ============


@pytest.mark.parametrize("state", ["partial", "unavailable", "unknown"])
def test_partial_or_unavailable_never_adjusts(state: str) -> None:
    """`availability.state != "available"` → 不修正（partial 不得当「未恶化」）。"""
    snap = _snapshot(close=3200.0, pct=-1.5, ratio=0.30, state=state)
    factors = _factors(FALLING, snap)
    assert intraday_available(snap) is False
    assert is_intraday_usable(snap, factors) is False
    assert apply_intraday_rules(3, factors, snap) == (3, [])


def test_intr_close_zero_is_unusable_and_never_breaks_down() -> None:
    """`intr_close == 0`（上游缺失被吞成 0）→ 不可用，绝不判跌破。"""
    snap = _snapshot(close=0.0, pct=-1.5, ratio=0.30)
    factors = _factors(FALLING, snap)
    assert factors.intr_close == 0.0
    # 点位无效 → 不估算均线（用 0 拖低 ma 会伪造破位）
    assert factors.intr_ma20 is None
    assert is_intraday_usable(snap, factors) is False
    assert apply_intraday_rules(4, factors, snap) == (4, [])


def test_factors_persist_with_nulls_when_unavailable() -> None:
    """因子恒落库：快照缺失时字段存在且为 null，键结构与 spec §4.4 一致。"""
    factors = derive_intraday_factors(None, RISING)
    payload = factors.model_dump()
    assert set(payload) == FACTOR_KEYS  # ma5/ma10 不落库
    assert payload["intr_close"] is None
    assert payload["intr_pct"] is None
    assert payload["intr_ratio"] is None
    assert payload["intr_ma20"] is None
    assert payload["mainline_pct"] is None
    assert payload["triggered_rules"] == []
    assert payload["threshold_version"]


# ============ 10-12. 取数与估算口径 ============


def test_mainline_pct_matches_loser_by_name() -> None:
    """主线涨跌幅按 `pct_change` 取值（不是 `pct_chg`），在 losers 中匹配。"""
    snap = _snapshot(
        losers=[{
            "name": "半导体", "pct_chg": -9.9,   # 干扰项：字段名不是它
            "pct_change": -2.8,
        }],
    )
    factors = _factors(RISING, snap, mainline_name="半导体")
    assert factors.mainline_pct == pytest.approx(-2.8)


def test_mainline_pct_none_when_not_found() -> None:
    """找不到主线板块 / 未传主线名 → None（不用指数涨跌幅冒充）。"""
    snap = _snapshot(gainers=[{"name": "银行", "pct_change": 1.1}])
    assert _factors(RISING, snap, mainline_name="半导体").mainline_pct is None
    assert _factors(RISING, snap).mainline_pct is None


def test_mainline_pct_matches_gainer() -> None:
    """gainers 侧同样按名匹配（规则 C 只认跌幅阈值，涨的不会触发）。"""
    snap = _snapshot(gainers=[{"name": "AI 算力", "pct_change": 0.8}])
    factors = _factors(RISING, snap, mainline_name="AI 算力")
    assert factors.mainline_pct == pytest.approx(0.8)
    assert apply_intraday_rules(2, factors, snap) == (2, [])


def test_intr_ma20_uses_temp_bar_estimate() -> None:
    """ma20 把盘中点位当临时 bar 追加后估算（不是真实收盘）。"""
    closes = [1000.0] * 19
    factors = _factors(closes, _snapshot(close=1200.0, pct=1.0, ratio=0.7))
    # (19 × 1000 + 1200) / 20
    assert factors.intr_ma20 == pytest.approx(1010.0)
    assert factors.intr_ma5 == pytest.approx((4 * 1000 + 1200) / 5)
    assert factors.intr_ma10 == pytest.approx((9 * 1000 + 1200) / 10)


def test_intr_ma20_none_when_bars_insufficient() -> None:
    """历史 K 线不足 20 根（含临时 bar 仍不足）→ None，不缩短窗口。"""
    factors = _factors([1000.0] * 10, _snapshot(close=1010.0))
    assert factors.intr_ma20 is None


def test_archive_flat_fields_take_precedence_over_raw() -> None:
    """归档形态：扁平派生字段优先，raw 只在缺失时兜底。"""
    raw = _snapshot(close=3000.0, pct=-5.0, ratio=0.1)
    factors = _factors(
        RISING, _archive(close=3250.0, pct=0.4, ratio=0.6, raw=raw)
    )
    assert factors.intr_close == 3250.0
    assert factors.intr_pct == pytest.approx(0.4)
    assert factors.intr_ratio == pytest.approx(0.6)


def test_archive_falls_back_to_raw_when_flat_fields_missing() -> None:
    """归档扁平字段为 null（取数失败的样本）→ 从 raw 兜底。"""
    raw = _snapshot(close=3210.0, pct=-0.9, ratio=0.42)
    factors = _factors(RISING, _archive(close=None, pct=None, ratio=None, raw=raw))
    assert factors.intr_close == 3210.0
    assert factors.intr_pct == pytest.approx(-0.9)
    assert factors.intr_ratio == pytest.approx(0.42)


# ============ 归档优先 / 实时回退 ============


@pytest.mark.asyncio
async def test_load_snapshot_prefers_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归档存在 → 直接读文件，不再请求实时端点（正常路径只有一次请求）。"""
    monkeypatch.setattr(wm.intraday_archive, "archive_dir", tmp_path)
    archived = _archive(close=3412.0, pct=-1.1, ratio=0.33, raw=_snapshot())
    (tmp_path / f"{TARGET}-1225.json").write_text(
        json.dumps(archived), encoding="utf-8"
    )
    fetch = AsyncMock(return_value=_snapshot(close=9999.0))
    monkeypatch.setattr(wm.node_api, "get_intraday_sectors", fetch)

    snapshot = await wm._load_intraday_snapshot(TARGET)

    assert snapshot is not None
    assert snapshot["intr_close"] == 3412.0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_snapshot_falls_back_to_live_fetch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归档缺失 → 回退实时取数。"""
    monkeypatch.setattr(wm.intraday_archive, "archive_dir", tmp_path)
    fetch = AsyncMock(return_value=_snapshot(close=3412.0))
    monkeypatch.setattr(wm.node_api, "get_intraday_sectors", fetch)

    snapshot = await wm._load_intraday_snapshot(TARGET)

    assert snapshot is not None
    assert snapshot["indexes"][1]["close"] == 3412.0
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_snapshot_returns_none_when_both_sources_fail(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归档缺失 + 实时取数失败 → None（由调用方按不可用留痕）。"""
    monkeypatch.setattr(wm.intraday_archive, "archive_dir", tmp_path)

    async def _raise() -> object:
        raise RuntimeError("node api down")

    monkeypatch.setattr(wm.node_api, "get_intraday_sectors", _raise)

    assert await wm._load_intraday_snapshot(TARGET) is None


# ============ worker midday 分支（端到端） ============


def _kline(rising: bool = True, n_rows: int = 40) -> list[dict[str, object]]:
    """K 线：rising=True 均线多头（A 不触发），False 空头（A 触发）。"""
    end = date_cls.fromisoformat(BASIS_DATE)
    rows: list[dict[str, object]] = []
    for i in range(n_rows):
        day = end - timedelta(days=n_rows - 1 - i)
        close = (3000.0 + i) if rising else (4000.0 - i * 20.0)
        rows.append({
            "trade_date": day.strftime("%Y%m%d"),
            "open": close, "high": close + 10, "low": close - 10,
            "close": close, "pct_chg": 0.0, "amount": 100.0,
        })
    return rows


def _items() -> list[LedgerItem]:
    return [
        LedgerItem(key="trend", value_text="3000.00 / ma20 2990.00", judgement="震荡",
                   effect="base=2", status="neutral"),
        LedgerItem(key="mainline", value_text="无主线数据", judgement="数据不可用",
                   effect="不改档", status="missing"),
        LedgerItem(key="event", value_text="无事件 / d 不可用", judgement="数据缺失",
                   effect="不改档", status="missing"),
        LedgerItem(key="breadth", value_text="上涨占比 50%", judgement="中性",
                   effect="仅参与 certainty，不改档", status="neutral"),
    ]


def _ledger_for(index: int) -> PositionLedger:
    return PositionLedger(
        items=_items(),
        base_index=min(index, 2),
        adjust=max(0, index - 2),
        final_index=index,
        gate_hit=False,
        mainline_breakdown=False,
        insufficient=False,
        gate_blocked=False,
        mainline_state="unavailable",
        watch_points=[],
        ledger_version="1.0",
    )


def _basis_report(index: int) -> dict[str, object]:
    ledger = _ledger_for(index)
    return {"content": {
        "target_date": TARGET,
        "basis_date": BASIS_DATE,
        "refresh_slot": "morning",
        "evidence": {"stage": "ebb", "stage_reason": "盘前基准：退潮"},
        "rhythm_card": {
            "position_band": {"text": "建议仓位：" + engine.POSITION_LADDER[index]},
            "position_index": index,
            "position_ledger": ledger.model_dump(),
        },
    }}


async def _compose_midday(
    snapshot: dict[str, object] | None, *, basis_index: int = 3, rising: bool = True,
) -> tuple[dict[str, object], IntradayFactors | None]:
    """跑 midday `_compose_card` + `_build_rhythm_card`，返回 (rhythm_card, 因子)。"""
    win_stub = type("W", (), {
        "events": [], "display_events": [], "high_events": [],
        "source_missing": False, "calendar_uncovered": False,
    })()
    patches = [
        patch.object(wm.node_api, "get_index_kline",
                     AsyncMock(return_value=_kline(rising=rising))),
        patch.object(wm.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})),
        patch.object(
            wm.node_api, "get_close_snapshot",
            AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
        ),
        patch.object(wm.node_api, "get_rhythm_report",
                     AsyncMock(return_value=_basis_report(basis_index))),
        patch.object(wm, "load_mainline_candidates", return_value=(False, [])),
        patch.object(wm, "load_event_timeline", AsyncMock(return_value=win_stub)),
        patch.object(wm, "_load_intraday_snapshot", AsyncMock(return_value=snapshot)),
        patch.object(wm, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(wm, "validate_synthesis", return_value=False),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        card, rows, win, source, factors = await wm._compose_card(TARGET, "midday")
    rhythm_card = wm._build_rhythm_card(
        card, win, rows, card.mainline_facts,
        position_source_hint=source, intraday_factors=factors,
    )
    return rhythm_card, factors


@pytest.mark.asyncio
async def test_midday_downgrade_syncs_ledger_watch_points_and_source() -> None:
    """盘中命中规则 B → 档位 3→2，账本 / 观察点 / 来源同步，因子落库含命中代号。"""
    snap = _snapshot(close=3250.0, pct=-1.2, ratio=0.30)
    rhythm_card, factors = await _compose_midday(snap, basis_index=3)

    assert rhythm_card["position_index"] == 2
    assert rhythm_card["position_source"] == "intraday_adjusted"
    assert rhythm_card["position_band"]["text"] == "建议仓位：" + engine.POSITION_LADDER[2]
    ledger = rhythm_card["position_ledger"]
    assert ledger["final_index"] == 2
    assert ledger["adjust"] == _ledger_for(3).adjust - 1
    # 档位变了 → 观察点按新档位重算（基准账本 watch_points 为空，重算后应产出条目）
    assert ledger["watch_points"]
    assert any("盘中走弱" in m for m in rhythm_card["data_missing"])
    # 因子恒落库：触发规则写入
    payload = rhythm_card["phase_evidence"]["intraday_factors"]
    assert factors is not None
    assert payload["triggered_rules"] == ["B"]
    assert payload["intr_close"] == pytest.approx(3250.0)
    assert payload["threshold_version"] == "v1-20261104"
    assert set(payload) == FACTOR_KEYS


@pytest.mark.asyncio
async def test_midday_no_worsening_keeps_inherited_basis() -> None:
    """盘中可见但未触发 → 档位不变，来源保持 inherited_basis，留「未见恶化」。"""
    snap = _snapshot(close=3245.0, pct=0.4, ratio=0.60)
    rhythm_card, _ = await _compose_midday(snap, basis_index=3)

    assert rhythm_card["position_index"] == 3
    assert rhythm_card["position_source"] == "inherited_basis"
    assert rhythm_card["position_ledger"]["final_index"] == 3
    assert "盘中未见恶化" in rhythm_card["data_missing"]
    assert rhythm_card["phase_evidence"]["intraday_factors"]["triggered_rules"] == []


@pytest.mark.asyncio
async def test_midday_unavailable_keeps_index_and_marks_source() -> None:
    """快照不可用 → 档位等于 morning 值，来源 intraday_unavailable，因子为 null。"""
    rhythm_card, _ = await _compose_midday(None, basis_index=4)

    assert rhythm_card["position_index"] == 4
    assert rhythm_card["position_source"] == "intraday_unavailable"
    assert "盘中数据不可用，仓位这次没有跟着改" in rhythm_card["data_missing"]
    payload = rhythm_card["phase_evidence"]["intraday_factors"]
    assert payload is not None
    assert payload["intr_close"] is None
    assert payload["triggered_rules"] == []


@pytest.mark.asyncio
async def test_midday_rule_a_end_to_end_from_four_to_zero() -> None:
    """规则 A：4 档基准 + 盘中空头排列跌破 ma20 → 直达 0 档。"""
    snap = _snapshot(close=3200.0, pct=-1.5, ratio=0.60)
    rhythm_card, _ = await _compose_midday(snap, basis_index=4, rising=False)

    assert rhythm_card["position_index"] == 0
    assert rhythm_card["position_source"] == "intraday_adjusted"
    assert rhythm_card["position_ledger"]["final_index"] == 0
    assert rhythm_card["phase_evidence"]["intraday_factors"]["triggered_rules"] == ["A"]


@pytest.mark.asyncio
async def test_after_close_card_has_no_intraday_factors() -> None:
    """其他时点不产出因子值（键存在且为 None，契约稳定）。"""
    win_stub = type("W", (), {
        "events": [], "display_events": [], "high_events": [],
        "source_missing": False, "calendar_uncovered": False,
    })()
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            wm.node_api, "get_index_kline", AsyncMock(return_value=_kline())
        ))
        stack.enter_context(patch.object(
            wm.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})
        ))
        stack.enter_context(patch.object(
            wm.node_api, "get_close_snapshot",
            AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
        ))
        stack.enter_context(patch.object(
            wm.node_api, "get_rhythm_report", AsyncMock(return_value=None)
        ))
        stack.enter_context(patch.object(
            wm, "load_mainline_candidates", return_value=(False, [])
        ))
        stack.enter_context(patch.object(wm, "load_event_timeline", AsyncMock(return_value=win_stub)))
        stack.enter_context(patch.object(wm, "run_synthesis", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(wm, "validate_synthesis", return_value=False))
        card, rows, win, source, factors = await wm._compose_card(BASIS_DATE, "after_close")

    assert factors is None
    rhythm_card = wm._build_rhythm_card(
        card, win, rows, card.mainline_facts,
        position_source_hint=source, intraday_factors=factors,
    )
    assert rhythm_card["phase_evidence"]["intraday_factors"] is None
