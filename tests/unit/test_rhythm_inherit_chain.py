"""P4 继承链串联 + morning 事件修正（职责重定义 spec §4.1 / §4.2 / §8）。

核心不变式：
- 继承链是**串联**的：after_close → morning → midday，morning 的事件修正不会被
  midday 从 after_close 重新继承而覆盖；
- `|morning − after_close| ≤ 1`、`midday ≤ morning`；
- morning 只做事件 ±1，趋势/量能/宽度维度一律继承；档位变了则账本与观察点同步。
"""

from contextlib import ExitStack
from datetime import date as date_cls
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import rhythm_master as wm
from aistock_agent.schemas.rhythm_master import (
    LedgerItem,
    PositionLedger,
    WatchPoint,
)
from aistock_agent.services import rhythm_engine as engine

TARGET = "2026-11-03"
BASIS_DATE = "2026-11-02"


def _kline(n_rows: int = 60, last: str = "20261102") -> list[dict[str, object]]:
    """震荡 K 线（base=2、无闸门），末日 = last（YYYYMMDD）。"""
    end = date_cls.fromisoformat(f"{last[:4]}-{last[4:6]}-{last[6:]}")
    rows: list[dict[str, object]] = []
    for i in range(n_rows):
        day = end - timedelta(days=n_rows - 1 - i)
        close = 3000.0 + (i % 7)
        rows.append({
            "trade_date": day.strftime("%Y%m%d"),
            "open": 3000.0, "high": close + 10, "low": close - 10,
            "close": close, "pct_chg": 0.0, "amount": 100.0,
        })
    return rows


def _closes(rows: list[dict[str, object]]) -> list[float]:
    return [float(r["close"]) for r in rows]  # type: ignore[arg-type]


def _items(event_text: str = "无事件 / d 不可用") -> list[LedgerItem]:
    return [
        LedgerItem(key="trend", value_text="3000.00 / ma20 2990.00", judgement="震荡",
                   effect="base=2", status="neutral"),
        LedgerItem(key="mainline", value_text="无主线数据", judgement="数据不可用",
                   effect="不改档", status="missing"),
        LedgerItem(key="event", value_text=event_text, judgement="数据缺失",
                   effect="不改档", status="missing"),
        LedgerItem(key="breadth", value_text="上涨占比 50%", judgement="中性",
                   effect="仅参与 certainty，不改档", status="neutral"),
    ]


def _ledger_for(
    index: int,
    *,
    watch_points: list[WatchPoint] | None = None,
    event_applied_kind: str | None = None,
    event_off_index: int | None = None,
) -> PositionLedger:
    """构造指定档位的基准账本（无闸门、主线不可用），可选注入「旧观察点」/幂等标志。"""
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
        event_applied_kind=event_applied_kind,
        event_off_index=event_off_index,
        watch_points=watch_points or [],
        ledger_version="1.0",
    )


def _report(
    index: int | None,
    ledger: PositionLedger | None = None,
    *,
    slot: str = "after_close",
    stage: str = "ebb",
    stage_reason: str = "收盘基准：退潮",
) -> dict[str, object]:
    """节奏卡响应（`get_rhythm_report` 已解包信封，content 在顶层）。"""
    rhythm_card: dict[str, object] = {
        "position_band": {
            "text": (
                "建议仓位：" + engine.POSITION_LADDER[index]
                if index is not None and engine.LADDER_MIN <= index <= engine.LADDER_MAX
                else ""
            ),
        },
        "position_index": index,
    }
    if ledger is not None:
        rhythm_card["position_ledger"] = ledger.model_dump()
    return {"content": {
        "target_date": TARGET,
        "basis_date": BASIS_DATE,
        "refresh_slot": slot,
        "evidence": {"stage": stage, "stage_reason": stage_reason},
        "rhythm_card": rhythm_card,
    }}


def _router(reports: dict[str, object | None]) -> AsyncMock:
    async def _get(target_date: str, slot: str) -> object | None:
        return reports.get(slot)

    return AsyncMock(side_effect=_get)


async def _compose(
    slot: str,
    *,
    reports: dict[str, object | None] | None = None,
    events: list[dict[str, object]] | None = None,
    run_date: str = TARGET,
    strong_mainline: bool = False,
):
    """跑 `_compose_card` 并返回 `(card, rows, win, position_source)`。"""
    events = events or []
    win_stub = type("W", (), {
        "events": events,
        "display_events": events,
        "high_events": [e for e in events if e.get("importance") == "high"],
        "source_missing": False,
        "calendar_uncovered": False,
    })()
    patches = [
        patch.object(wm.node_api, "get_index_kline", AsyncMock(return_value=_kline())),
        patch.object(wm.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})),
        patch.object(
            wm.node_api, "get_close_snapshot",
            AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
        ),
        patch.object(wm.node_api, "get_rhythm_report", _router(reports or {})),
        patch.object(wm, "load_event_timeline", AsyncMock(return_value=win_stub)),
        patch.object(wm, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(wm, "validate_synthesis", return_value=False),
    ]
    if strong_mainline:
        candidates = [
            {"name": "AI 算力", "tag_code": "886050.TI", "aliases": []},
            {"name": "AI 应用", "tag_code": "886108.TI", "aliases": []},
            {"name": "半导体", "tag_code": "881121.TI", "aliases": []},
        ]
        patches += [
            patch.object(wm, "load_mainline_candidates", return_value=(True, candidates)),
            patch.object(wm.node_api, "get_ths_index_map", AsyncMock(return_value=[
                {"ts_code": c["tag_code"], "name": c["name"]} for c in candidates
            ])),
            patch.object(wm.node_api, "get_ths_daily_range", AsyncMock(return_value=[
                {"trade_date": "20261102", "pct_chg": 1.0} for _ in range(30)
            ])),
            patch.object(wm, "judge_mainline", return_value={
                "state": "established", "name": "AI 算力", "strength": "strong",
                "excess": 2.0, "data_date": BASIS_DATE, "attention": "",
                "breakdown": False, "nav": None,
            }),
        ]
    else:
        patches.append(patch.object(wm, "load_mainline_candidates", return_value=(False, [])))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        # 本文件只断言档位/账本；盘中取数固定为「不可用」，避免 midday 打真实端点。
        # 盘中降档链路见 tests/unit/test_rhythm_intraday.py。
        with patch.object(wm, "_load_intraday_snapshot", AsyncMock(return_value=None)):
            card, composed_rows, composed_win, source, _ = await wm._compose_card(
                run_date, slot
            )
        return card, composed_rows, composed_win, source


def _rhythm_card(card, rows, win, source) -> dict[str, object]:
    return wm._build_rhythm_card(
        card, win, rows, card.mainline_facts, position_source_hint=source,
    )


def _as_report(rhythm_card: dict[str, object], card, *, slot: str) -> dict[str, object]:
    """把产出的卡回灌成「读得到的上游报告」（模拟 midday 读 morning 卡）。"""
    return {"content": {
        "target_date": card.target_date,
        "basis_date": card.basis_date,
        "refresh_slot": slot,
        "evidence": card.evidence.model_dump(),
        "rhythm_card": rhythm_card,
    }}


# ============ 1. event_adjustment 契约 ============


@pytest.mark.parametrize(
    ("event_d", "event_result", "state", "strength", "gate_blocked", "base_index", "delta"),
    [
        # d∈{1,2} 且强主线 +1
        (1, None, "established", "strong", False, 3, 1),
        (2, None, "established", "strong", False, 2, 1),
        # 主线不强 → 0
        (1, None, "established", "weak", False, 3, 0),
        (1, None, "unavailable", None, False, 3, 0),
        # 闸门命中 → 0（封顶除外）
        (1, None, "established", "strong", True, 0, 0),
        # d==0 已落档：绝对定档 → delta = target − 主线增量
        (0, "超预期", "established", "strong", False, 3, 0),
        (0, "超预期", "unavailable", None, False, 3, 1),
        (0, "不及预期", "established", "strong", False, 3, -2),
        (0, "不及预期", "unavailable", None, False, 3, -1),
        (0, "符合", "established", "strong", False, 3, -1),
        # 闸门命中时事件落档不生效
        (0, "超预期", "established", "strong", True, 0, 0),
        # 事件窗口外 / 无事件 → 0
        (None, None, "established", "strong", False, 3, 0),
        (5, None, "established", "strong", False, 3, 0),
    ],
)
def test_event_adjustment_rule(
    event_d, event_result, state, strength, gate_blocked, base_index, delta
) -> None:
    got, _ = engine.event_adjustment(
        event_d=event_d, event_result=event_result,
        mainline_state=state, mainline_strength=strength,
        gate_blocked=gate_blocked, base_index=base_index,
    )
    assert got == delta


@pytest.mark.parametrize("base_index", [0, 1, 2, 3, 4])
def test_event_adjustment_cap_is_folded_into_delta(base_index: int) -> None:
    """d==0 未落档的「封顶 1」已折算进 delta：base + delta == min(base, 1)。"""
    delta, reason = engine.event_adjustment(
        event_d=0, event_result=None,
        mainline_state="established", mainline_strength="strong",
        gate_blocked=False, base_index=base_index,
    )
    assert reason == engine.EVENT_CAP_REASON
    assert base_index + delta == min(base_index, 1)


def test_event_adjustment_cap_applies_even_when_gate_blocked() -> None:
    """封顶在闸门判断之外仍生效（历史行为：K 线不足时 base=2 也要压到 1）。"""
    delta, reason = engine.event_adjustment(
        event_d=0, event_result=None,
        mainline_state="unavailable", mainline_strength=None,
        gate_blocked=True, base_index=2,
    )
    assert reason == engine.EVENT_CAP_REASON
    assert 2 + delta == 1


@pytest.mark.parametrize(
    ("event_d", "event_result", "mainline", "expected"),
    [
        (0, "超预期", {"state": "unavailable"}, "result"),   # 已按结果定档
        (0, "符合", {"state": "established", "strength": "strong"}, "result"),  # delta=0 也算
        (0, None, {"state": "established", "strength": "strong"}, None),  # 封顶＝结果未知
        (1, None, {"state": "established", "strength": "strong"}, "approaching"),  # 临近 +1
        (1, None, {"state": "established", "strength": "weak"}, None),     # 未生效
        (None, None, {"state": "established", "strength": "strong"}, None),  # 无事件
        (1, None, {"state": "established", "strength": "strong", "breakdown": True}, None),
    ],
    ids=["landed", "landed-zero-delta", "cap", "window-strong", "window-weak",
         "no-event", "gate-blocked"],
)
def test_derive_sets_event_applied_kind(event_d, event_result, mainline, expected) -> None:
    """账本 `event_applied_kind` 三态：封顶/闸门/未生效 → None，落档 → result，临近 → approaching。"""
    closes = [3000.0 + i * 5 for i in range(25)]
    _, ledger = engine.derive_position_index(
        index_closes=closes, mainline=mainline,
        event_d=event_d, event_result=event_result,
    )
    assert ledger.event_applied_kind == expected


# ============ 2. 抽取后 derive_position_index 判据不变 ============


def _legacy_derive(closes, mainline, event_d, event_result):  # noqa: ANN001
    """抽取前 step0-4 的原样副本（回归护栏：判据必须逐行等价）。"""
    bars = len(closes)
    trend_known = bars >= 20
    if trend_known:
        c = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        bull = c > ma5 > ma10 > ma20
        gate_hit = c < ma20 and ma5 < ma10 < ma20
    else:
        bull = gate_hit = False
    ml_breakdown = bool((mainline or {}).get("breakdown"))
    if gate_hit or ml_breakdown:
        base = 0
    elif bull:
        base = 3
    else:
        base = 2
    gate_blocked = gate_hit or ml_breakdown or bars < 20
    state = None
    raw_state = (mainline or {}).get("state")
    if raw_state in ("established", "none", "unavailable"):
        state = str(raw_state)
    elif raw_state is not None:
        state = "unavailable"
    strength = (mainline or {}).get("strength")
    adj = 0
    if not gate_blocked:
        if state == "established" and strength == "strong":
            adj += 1
        elif state == "none":
            base = 0
        if event_d is not None and event_d in (1, 2) \
                and state == "established" and strength == "strong":
            adj += 1
        elif event_d == 0:
            if event_result in engine.EVENT_RESULT_ENUM:
                direction = engine.EVENT_RESULT_DIRECTION[event_result]
                adj = 1 if direction == "bullish" else (-1 if direction == "bearish" else 0)
            else:
                base = min(base, 1)
    final = base + adj
    if event_d == 0 and event_result not in engine.EVENT_RESULT_ENUM:
        final = min(final, 1)
    return max(engine.LADDER_MIN, min(engine.LADDER_MAX, final))


@pytest.mark.parametrize(
    "closes",
    [
        [3000.0 + i * 5 for i in range(25)],      # 多头
        [float(3000 + (i % 7)) for i in range(30)],  # 震荡
        [3400.0 - i * 5 for i in range(25)],      # 空头闸门
        [3000.0] * 10,                             # K 线不足
    ],
    ids=["bull", "osc", "bear", "short"],
)
@pytest.mark.parametrize(
    "mainline",
    [
        None,
        {"state": "established", "name": "X", "strength": "strong"},
        {"state": "established", "name": "X", "strength": "weak"},
        {"state": "none"},
        {"state": "unavailable"},
        {"state": "established", "name": "X", "strength": "strong", "breakdown": True},
    ],
    ids=["none-input", "strong", "weak", "no-mainline", "unavailable", "breakdown"],
)
@pytest.mark.parametrize("event_d", [None, 0, 1, 2, 5])
@pytest.mark.parametrize("event_result", [None, "超预期", "符合", "不及预期"])
def test_derive_index_unchanged_after_extraction(closes, mainline, event_d, event_result) -> None:
    expected = _legacy_derive(closes, mainline, event_d, event_result)
    got, ledger = engine.derive_position_index(
        index_closes=closes, mainline=mainline,
        event_d=event_d, event_result=event_result,
    )
    assert got == expected
    assert ledger.final_index == expected
    # 账本算术与判定一致（[0,4] 上界/下界 clamp 由 derive 统一做）
    assert ledger.base_index is not None
    assert ledger.final_index == max(
        engine.LADDER_MIN, min(engine.LADDER_MAX, ledger.base_index + ledger.adjust)
    )


# ============ 3. 核心：morning 事件 +1 被 midday 继承 ============


@pytest.mark.asyncio
async def test_morning_event_plus_one_is_inherited_by_midday() -> None:
    """morning 因事件 +1 → midday 读 morning（不得回退 after_close 覆盖掉该修正）。"""
    basis_ledger = _ledger_for(2)
    basis = _report(2, basis_ledger)
    event = {
        "date": TARGET, "title": "隔夜利好", "importance": "high", "result": "超预期",
    }

    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    morning_card = _rhythm_card(card, rows, win, source)
    assert morning_card["position_index"] == 3
    assert morning_card["position_source"] == "event_adjusted"

    midday_reports = {
        "after_close": basis,
        "morning": _as_report(morning_card, card, slot="morning"),
    }
    card2, rows2, win2, source2 = await _compose("midday", reports=midday_reports)
    midday_card = _rhythm_card(card2, rows2, win2, source2)

    # 核心断言：midday 继承 morning 的 3 档，而不是 after_close 的 2 档
    assert midday_card["position_index"] == 3
    # 盘中数据不可用（helper 固定返回 None）→ 不修正，来源如实标注（spec §4.3）
    assert midday_card["position_source"] == "intraday_unavailable"
    assert midday_card["position_ledger"]["final_index"] == 3
    assert "沿用盘前基准" in card2.evidence.stage_reason
    # 回退留痕不得出现（这次是正常串联）
    assert wm.BASIS_FALLBACK_NOTE not in midday_card["data_missing"]


@pytest.mark.asyncio
async def test_morning_event_window_plus_one_with_strong_mainline() -> None:
    """d∈{1,2} + 主线成立·强 → morning +1（spec §4.2 第一条）。"""
    basis = _report(2, _ledger_for(2))
    event = {"date": "2026-11-04", "title": "议息", "importance": "high"}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event], strong_mainline=True,
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 3
    assert rhythm_card["position_source"] == "event_adjusted"
    assert rhythm_card["position_ledger"]["items"][2]["judgement"] == "事件支持加仓"


# ============ 4. midday 回退 ============


@pytest.mark.asyncio
async def test_midday_falls_back_to_after_close_when_morning_missing() -> None:
    basis = _report(2, _ledger_for(2))
    card, rows, win, source = await _compose("midday", reports={"after_close": basis})
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 2
    assert wm.BASIS_FALLBACK_NOTE in rhythm_card["data_missing"]
    assert wm.BASIS_FALLBACK_NOTE in card.evidence.data_missing


@pytest.mark.asyncio
async def test_midday_reads_morning_first_then_after_close() -> None:
    """串联次序：先读 morning，不可用才读 after_close。"""
    basis = _report(2, _ledger_for(2))
    router = _router({"after_close": basis})
    with patch.object(wm.node_api, "get_rhythm_report", router):
        await wm._resolve_basis_card("midday", TARGET)
    assert [c.args for c in router.await_args_list] == [
        (TARGET, "morning"), (TARGET, "after_close"),
    ]


@pytest.mark.asyncio
async def test_midday_falls_back_when_morning_index_is_null() -> None:
    """旧 morning 卡无 position_index → 回退收盘基准并留痕。"""
    basis = _report(2, _ledger_for(2))
    stale_morning = _report(None, slot="morning")
    card, rows, win, source = await _compose(
        "midday", reports={"after_close": basis, "morning": stale_morning},
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 2
    assert wm.BASIS_FALLBACK_NOTE in rhythm_card["data_missing"]


@pytest.mark.asyncio
async def test_midday_falls_back_when_morning_index_out_of_range() -> None:
    """morning 卡 position_index 越界（野值）→ 与缺失同语义回退，不 clamp 采信。"""
    basis = _report(2, _ledger_for(2))
    broken_morning = _report(9, slot="morning")
    card, rows, win, source = await _compose(
        "midday", reports={"after_close": basis, "morning": broken_morning},
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 2
    assert wm.BASIS_FALLBACK_NOTE in rhythm_card["data_missing"]


# ============ 5. 不变式：|morning − after_close| ≤ 1、midday ≤ morning ============

_EVENTS = {
    "none": [],
    "d0-unlanded": [{"date": TARGET, "title": "待公布", "importance": "high"}],
    "d0-beat": [{"date": TARGET, "title": "超预期", "importance": "high", "result": "超预期"}],
    "d0-miss": [{"date": TARGET, "title": "不及预期", "importance": "high", "result": "不及预期"}],
    "d0-meet": [{"date": TARGET, "title": "符合", "importance": "high", "result": "符合"}],
    "d1-window": [{"date": "2026-11-04", "title": "明日", "importance": "high"}],
}
# 安全封顶（d=0 未落档）不受 ±1 约束，单独一组断言
_CAP_EVENT = "d0-unlanded"
_NON_CAP_EVENTS = [k for k in _EVENTS if k != _CAP_EVENT]


@pytest.mark.asyncio
@pytest.mark.parametrize("basis_index", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("event_key", _NON_CAP_EVENTS)
async def test_morning_delta_never_exceeds_one(basis_index: int, event_key: str) -> None:
    """非封顶路径：事件方向调整恒 ≤1 档（spec §8.4）。"""
    basis = _report(basis_index, _ledger_for(basis_index))
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=_EVENTS[event_key],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] is not None
    assert abs(rhythm_card["position_index"] - basis_index) <= 1
    assert 0 <= rhythm_card["position_index"] <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("basis_index", [0, 1, 2, 3, 4])
async def test_morning_cap_is_not_limited_by_plus_minus_one(basis_index: int) -> None:
    """封顶路径：安全规则优先，`morning == min(after_close, 1)`，可一次下调多档。"""
    basis = _report(basis_index, _ledger_for(basis_index))
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=_EVENTS[_CAP_EVENT],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == min(basis_index, 1)
    if basis_index > 1:
        assert rhythm_card["position_source"] == "event_adjusted"
        assert any("封顶" in m for m in rhythm_card["data_missing"])


@pytest.mark.asyncio
@pytest.mark.parametrize("basis_index", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("event_key", list(_EVENTS))
async def test_midday_never_exceeds_morning(basis_index: int, event_key: str) -> None:
    basis = _report(basis_index, _ledger_for(basis_index))
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=_EVENTS[event_key],
    )
    morning_card = _rhythm_card(card, rows, win, source)
    card2, rows2, win2, source2 = await _compose(
        "midday",
        reports={
            "after_close": basis,
            "morning": _as_report(morning_card, card, slot="morning"),
        },
        events=_EVENTS[event_key],
    )
    midday_card = _rhythm_card(card2, rows2, win2, source2)
    assert midday_card["position_index"] == morning_card["position_index"]
    assert midday_card["position_index"] <= morning_card["position_index"]


# ============ 6. morning 事件修正的账本同步 ============


@pytest.mark.asyncio
async def test_morning_event_adjusted_ledger_is_synced() -> None:
    """档位变了 → adjust / final_index / event 行 / watch_points 全部按新档位重算。"""
    stale_point = WatchPoint(
        key="event", condition="旧观察点", current_text="旧", sufficient_alone=False,
    )
    basis_ledger = _ledger_for(2, watch_points=[stale_point])
    basis = _report(2, basis_ledger)
    event = {"date": TARGET, "title": "隔夜利好", "importance": "high", "result": "超预期"}

    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)

    assert rhythm_card["position_source"] == "event_adjusted"
    assert rhythm_card["position_index"] == 3
    ledger_payload = rhythm_card["position_ledger"]
    assert ledger_payload["final_index"] == rhythm_card["position_index"]
    assert ledger_payload["adjust"] == basis_ledger.adjust + 1
    assert ledger_payload["items"][2]["judgement"] == "事件支持加仓"
    # 观察点按新档位重算，不再是基准卡的旧观察点
    assert ledger_payload["watch_points"] != [stale_point.model_dump()]
    ledger = card.evidence.position_ledger
    assert ledger is not None
    assert [p.model_dump() for p in ledger.watch_points] == ledger_payload["watch_points"]
    # 溯源文案与同一份账本同源
    from aistock_agent.services.rhythm_position_ledger import render_causality

    assert rhythm_card["causality_text"] == render_causality(ledger).model_dump()


@pytest.mark.asyncio
async def test_morning_cap_breaks_plus_minus_one_bound() -> None:
    """安全封顶优先于 ±1：4 档遇 d=0 未落档 → 完整封顶到 1（spec §4.2 裁决）。"""
    basis = _report(4, _ledger_for(4))
    event = [{"date": TARGET, "title": "待公布", "importance": "high"}]
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=event,
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 1
    assert rhythm_card["position_source"] == "event_adjusted"
    assert any(
        "封顶" in m and "4" in m and "1" in m for m in rhythm_card["data_missing"]
    ), rhythm_card["data_missing"]
    ledger = card.evidence.position_ledger
    assert ledger is not None
    assert ledger.event_applied_kind is None  # 封顶是「结果未知」的安全约束，不算已计入


@pytest.mark.asyncio
async def test_morning_skips_event_when_basis_already_applied_result() -> None:
    """幂等：after_close 已按同一条 result 定过档（kind="result"）→ morning 不再计一次。"""
    basis_ledger = _ledger_for(3, event_applied_kind="result")
    basis = _report(3, basis_ledger)
    event = {"date": TARGET, "title": "超预期", "importance": "high", "result": "超预期"}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 3  # 不得变成 4（同一事件计两次）
    assert rhythm_card["position_source"] == "inherited_basis"
    assert rhythm_card["position_ledger"]["event_applied_kind"] == "result"
    assert any("不重复调整" in m for m in rhythm_card["data_missing"])


@pytest.mark.asyncio
async def test_morning_cap_can_be_unlocked_by_new_result() -> None:
    """封顶未计入（kind=None）→ 盘前拿到结果后仍能正确应用 +1。"""
    basis_ledger = _ledger_for(1, event_applied_kind=None)
    basis = _report(1, basis_ledger)
    event = {"date": TARGET, "title": "超预期", "importance": "high", "result": "超预期"}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 2
    assert rhythm_card["position_source"] == "event_adjusted"
    assert rhythm_card["position_ledger"]["event_applied_kind"] == "result"
    assert rhythm_card["position_ledger"]["items"][2]["judgement"] == "事件支持加仓"


# ============ 6b. approaching → 撤销临近 +1 后按结果重算（坏消息必须能降档）============


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_index"),
    [("不及预期", 1), ("符合", 2), ("超预期", 3)],
)
async def test_morning_revokes_approaching_add_then_applies_result(
    result: str, expected_index: int,
) -> None:
    """after_close=3 且 kind="approaching" → 先撤销该 +1（start=event_off_index=2）再按结果定档。"""
    basis_ledger = _ledger_for(3, event_applied_kind="approaching", event_off_index=2)
    basis = _report(3, basis_ledger)
    event = {"date": TARGET, "title": "隔夜事件", "importance": "high", "result": result}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == expected_index
    # 净变化为 0（超预期：−1 撤销 +1 再加回）时档位来源仍是继承
    expected_source = "event_adjusted" if expected_index != 3 else "inherited_basis"
    assert rhythm_card["position_source"] == expected_source
    # 结果已落档 → 幂等标志升级为 result，后续时点不再重复计
    assert rhythm_card["position_ledger"]["event_applied_kind"] == "result"
    assert rhythm_card["position_ledger"]["final_index"] == expected_index
    # 事件行按盘前看到的新事实改写（不得仍写「d=1 事件临近」）
    event_row = rhythm_card["position_ledger"]["items"][2]
    assert event_row["value_text"] == f"事件当天，结果{result}"


@pytest.mark.asyncio
async def test_morning_revoke_clamps_start_at_zero() -> None:
    """event_off_index=0 且 kind="approaching" → start 恒 0，不得负（撤 +1 后按结果降）。"""
    basis_ledger = _ledger_for(1, event_applied_kind="approaching", event_off_index=0)
    basis = _report(1, basis_ledger)
    event = {"date": TARGET, "title": "隔夜事件", "importance": "high", "result": "不及预期"}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 0  # start=0 → delta −1 → clamp 0
    assert rhythm_card["position_source"] == "event_adjusted"


def _derived_report(closes: list[float], mainline: dict[str, object]) -> tuple[dict, int]:
    """用真实 derive 产出基准卡（event_d=1 临近事件），返回 (report, 序号)。"""
    index, ledger = engine.derive_position_index(
        index_closes=closes, mainline=mainline,
        event_d=1, event_result=None,
    )
    return _report(index, ledger), index


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closes", "expected_index", "expected_off", "result", "expected_morning"),
    [
        # clamp 生效：base3 + 主线1 + 临近1 = 5 → 4，事件前档位仍是 4
        ([3000.0 + i * 5 for i in range(25)], 4, 4, "符合", 4),
        ([3000.0 + i * 5 for i in range(25)], 4, 4, "不及预期", 3),
        # clamp 未生效：base2 + 主线1 + 临近1 = 4，事件前档位 3
        ([float(3000 + (i % 7)) for i in range(30)], 4, 3, "符合", 3),
        ([float(3000 + (i % 7)) for i in range(30)], 4, 3, "不及预期", 2),
    ],
    ids=["clamped-meet", "clamped-miss", "unclamped-meet", "unclamped-miss"],
)
async def test_morning_revoke_uses_event_off_index(
    closes, expected_index, expected_off, result, expected_morning,
) -> None:
    """撤销基线取 `event_off_index`：4 档上界吃掉 +1 时不得多撤一档。"""
    strong = {"state": "established", "name": "X", "strength": "strong"}
    basis, index = _derived_report(closes, strong)
    assert index == expected_index
    assert basis["content"]["rhythm_card"]["position_ledger"]["event_off_index"] == expected_off
    assert basis["content"]["rhythm_card"]["position_ledger"]["event_applied_kind"] == "approaching"

    event = {"date": TARGET, "title": "隔夜事件", "importance": "high", "result": result}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == expected_morning


@pytest.mark.asyncio
async def test_morning_revoke_falls_back_when_event_off_index_missing() -> None:
    """旧基准卡无 event_off_index → 回退 `继承序号 − 1` 并留痕，不得崩。"""
    basis_ledger = _ledger_for(3, event_applied_kind="approaching")  # event_off_index=None
    basis = _report(3, basis_ledger)
    event = {"date": TARGET, "title": "隔夜事件", "importance": "high", "result": "符合"}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 2  # start = 3 − 1，符合 → 2
    assert rhythm_card["position_source"] == "event_adjusted"
    assert any("旧基准卡无事件前档位" in m for m in rhythm_card["data_missing"])


# ============ 7. 无事件修正 → inherited_basis ============


@pytest.mark.asyncio
async def test_morning_without_event_keeps_inherited_basis() -> None:
    basis = _report(2, _ledger_for(2))
    card, rows, win, source = await _compose("morning", reports={"after_close": basis})
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] == 2
    assert rhythm_card["position_source"] == "inherited_basis"
    assert rhythm_card["position_ledger"] == basis["content"]["rhythm_card"][
        "position_ledger"
    ]


@pytest.mark.asyncio
async def test_morning_keeps_null_index_for_old_basis_card() -> None:
    """旧基准卡无档位 → 不修正、保持 null，只留痕（不得按文案反推）。"""
    basis = _report(None)
    event = {"date": TARGET, "title": "隔夜利好", "importance": "high", "result": "超预期"}
    card, rows, win, source = await _compose(
        "morning", reports={"after_close": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert rhythm_card["position_index"] is None
    assert rhythm_card["position_source"] == "inherited_basis"
    assert any("基准卡无有效档位" in m for m in rhythm_card["data_missing"])


# ============ 8. 本地重算兜底不受影响 ============


@pytest.mark.asyncio
async def test_after_close_still_computes_locally() -> None:
    """after_close 不继承：仍走 derive_position_index 本地定档。"""
    card, rows, win, source = await _compose("after_close")
    rhythm_card = _rhythm_card(card, rows, win, source)
    assert source is None
    assert rhythm_card["position_source"] == "after_close_base"
    assert rhythm_card["position_index"] is not None
    closes = _closes(_kline())
    expected, _ = engine.derive_position_index(
        index_closes=closes, mainline=card.mainline_facts or {},
        event_d=None, event_result=None,
        breadth={"total_count": 100, "advance_count": 50},
    )
    assert rhythm_card["position_index"] == expected


@pytest.mark.asyncio
async def test_same_day_event_without_result_caps_inherited_index() -> None:
    """事件当期（d=0）结果未落档 → 继承来的高档位也必须封顶 1。

    场景来源（远端 be1c558）：after_close 定档时事件尚未入库（盘后财报等），
    morning/midday 沿用基准档位时不能因「沿用」被放过；11:30 / 13:00 的预期差
    扫描也可能让 midday 才看到当天的事件。

    实现方式与 be1c558 不同：不在文案层反推档位（那是本次重构拆掉的反向推断），
    而是在档位层面按本卡自己的 event_d/event_result 兜一次出口闸。
    """
    basis = _report(3, _ledger_for(3))
    event = {"date": TARGET, "title": "盘后财报", "importance": "high"}
    card, rows, win, source = await _compose(
        "midday", reports={"morning": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)

    assert rhythm_card["position_index"] == 1
    assert rhythm_card["position_ledger"]["final_index"] == 1
    assert any("封顶" in str(m) for m in rhythm_card["data_missing"])


@pytest.mark.asyncio
async def test_same_day_event_with_result_does_not_cap() -> None:
    """结果已落档 → 不该触发封顶（否则等于把已兑现的事件当未知风险）。"""
    basis = _report(3, _ledger_for(3))
    event = {
        "date": TARGET, "title": "盘后财报", "importance": "high", "result": "符合",
    }
    card, rows, win, source = await _compose(
        "midday", reports={"morning": basis}, events=[event],
    )
    rhythm_card = _rhythm_card(card, rows, win, source)

    assert rhythm_card["position_index"] != 1 or "封顶" not in str(
        rhythm_card["data_missing"]
    )
