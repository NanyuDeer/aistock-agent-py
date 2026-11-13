"""stage_source 判定路径透出（问题 1）与中性沿用时的仓位降级（问题 3）。"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.utils.date import shanghai_today


def _dates(n_rows: int) -> list[str]:
    """以今天为末日的连续自然日（basis_gate 只比对末日，盘中未完成 bar 剔除另有覆盖）。"""
    today = shanghai_today()
    return [(today - timedelta(days=n_rows - 1 - i)).strftime("%Y%m%d") for i in range(n_rows)]


def _mild_kline(n_rows: int = 60):
    """均线多头但末段缩量：trend +1.5、vol −1 → 综合分 +0.5，正落判定死区（-0.5, 1）。"""
    dates = _dates(n_rows)
    rows = []
    for i, d in enumerate(dates):
        close = 3000.0 + i * 10
        rows.append({
            "trade_date": d,
            "open": close - 5,
            "high": close + 10,
            "low": close - 10,
            "close": close,
            "pct_chg": 0.5,
            # 最后 5 日量能骤降：avg5/avg20 ≈ 0.13 → volume_score = -1
            "amount": 10.0 if i >= n_rows - 5 else 100.0,
        })
    return rows


def _flat_kline(n_rows: int = 60):
    """横盘 K 线：均线粘合、量能平稳 → trend/vol 都为 0，便于构造"中性"场景。"""
    return [
        {
            "trade_date": d,
            "open": 3000.0,
            "high": 3001.0,
            "low": 2999.0,
            "close": 3000.0,
            "pct_chg": 0.0,
            "amount": 100.0,
        }
        for d in _dates(n_rows)
    ]


def _bull_kline(n_rows: int = 60):
    return [
        {
            "trade_date": d,
            "open": 3000.0 + i * 10 - 5,
            "high": 3000.0 + i * 10 + 10,
            "low": 3000.0 + i * 10 - 10,
            "close": 3000.0 + i * 10,
            "pct_chg": 0.5,
            "amount": 100.0 + i,
        }
        for i, d in enumerate(_dates(n_rows))
    ]


def _prev_report(stage: str, basis_date: str, reason: str = "温度回落/量能转弱，退潮"):
    return {
        "content": {
            "basis_date": basis_date,
            "evidence": {"stage": stage, "stage_reason": reason},
        }
    }


def _prev_report_with_band(
    stage: str,
    basis_date: str,
    position_text: str,
    reason: str = "温度回落/量能转弱，退潮",
):
    return {
        "content": {
            "basis_date": basis_date,
            "evidence": {"stage": stage, "stage_reason": reason},
            "rhythm_card": {"position_band": {"text": position_text}},
        }
    }


def _patches(kline, breadth, fg, rhythm_report):
    return (
        patch(
            "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
            AsyncMock(return_value=kline),
        ),
        patch(
            "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
            AsyncMock(return_value={"index": fg}),
        ),
        patch(
            "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
            AsyncMock(return_value={"breadth": breadth}),
        ),
        patch(
            "aistock_agent.agents.workers.rhythm_master.node_api.get_rhythm_report",
            AsyncMock(return_value=rhythm_report),
        ),
        patch(
            "aistock_agent.agents.workers.rhythm_master.load_event_timeline",
            AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
        ),
        patch(
            "aistock_agent.agents.workers.rhythm_master.run_synthesis",
            AsyncMock(return_value=None),
        ),
        patch(
            "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
        ),
    )


async def _run_compose_full(slot: str, kline, breadth, fg, rhythm_report):
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    ps = _patches(kline, breadth, fg, rhythm_report)
    vs = ps[-1]
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], vs:
        vs.return_value = True
        card, rows, win, source, _intraday = await _compose_card(
            shanghai_today().isoformat(), slot
        )
        return card, rows, win, source


async def _run_compose(slot: str, kline, breadth, fg, rhythm_report):
    card, _, _, _ = await _run_compose_full(slot, kline, breadth, fg, rhythm_report)
    return card


@pytest.mark.asyncio
async def test_stage_source_computed_when_local_stage_decided():
    # 单边上行 + 放量 + 宽度扩张 + 恐贪高位 → 本地判出方向，非沿用
    card = await _run_compose(
        "after_close",
        _bull_kline(),
        {"total_count": 100, "advance_count": 90},
        75,
        None,
    )
    assert card.evidence.stage is not None
    assert card.evidence.stage_source == "computed"


@pytest.mark.asyncio
async def test_stage_source_inherited_prev_day_when_neutral_and_prev_available():
    # 均线多头但缩量 → 综合分 +0.5 落死区，沿用前一交易日 after_close 卡
    # 基日必须相对「今天」取：resolve_prev_phase 有 max_gap_days=3 的时效闸，
    # 硬编码 2026-10-30 会随真实日期推移超过窗口而必然被拒（时间炸弹）。
    prev = _prev_report("ebb", shanghai_today().isoformat())
    card = await _run_compose(
        "after_close",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        prev,
    )
    assert card.evidence.stage == "ebb"
    assert card.evidence.stage_source == "inherited_prev_day"
    assert "沿用前阶段" in card.evidence.stage_reason


@pytest.mark.asyncio
async def test_stage_source_inherited_basis_for_morning():
    # 盘前卡沿用同日收盘基准 → 与"沿用前一日"区分
    basis = _prev_report("ebb", shanghai_today().isoformat())
    card = await _run_compose(
        "morning",
        _flat_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        basis,
    )
    assert card.evidence.stage_source == "inherited_basis"


@pytest.mark.asyncio
async def test_stage_source_none_when_stage_missing():
    # K 线不足 → stage=None，判定路径也应为 None（不伪造来源）
    card = await _run_compose(
        "after_close",
        _flat_kline(5),
        {"total_count": 100, "advance_count": 50},
        45,
        None,
    )
    assert card.evidence.stage is None
    assert card.evidence.stage_source is None


@pytest.mark.asyncio
async def test_position_band_single_source_when_inherited_prev_day():
    """2026-11-04 单一真相源：档位沿用前一交易日时，仓位仍由 derive_position_index 产出
    （不再按 stage 查表降级），文案必须等于 POSITION_LADDER[position_index]。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card
    from aistock_agent.services.rhythm_engine import POSITION_LADDER

    # 基日必须相对「今天」取：resolve_prev_phase 有 max_gap_days=3 的时效闸，
    # 硬编码 2026-10-30 会随真实日期推移超过窗口而必然被拒（时间炸弹）。
    prev = _prev_report("ebb", shanghai_today().isoformat())
    card, rows, win, _ = await _run_compose_full(
        "after_close",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        prev,
    )
    assert card.evidence.stage_source == "inherited_prev_day"
    # evidence.position 与账本同源：index 必可追溯，账本 final_index 与之一致
    assert card.evidence.position is not None
    pos_index = card.evidence.position.index
    assert pos_index is not None
    assert card.evidence.position_ledger is not None
    assert card.evidence.position_ledger.final_index == pos_index
    assert card.evidence.position.text == POSITION_LADDER[pos_index]

    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["phase_evidence"]["stage_source"] == "inherited_prev_day"
    assert rhythm_card["position_source"] == "inherited_prev_day"
    assert rhythm_card["position_index"] == pos_index
    assert rhythm_card["position_band"]["text"] == "建议仓位：" + POSITION_LADDER[pos_index]
    assert rhythm_card["position_ledger"] is not None
    assert rhythm_card["position_ledger"]["final_index"] == pos_index


@pytest.mark.asyncio
async def test_position_band_keeps_gate_when_stage_computed():
    """硬判场景仍走 derive_position_index（破位闸门语义不被削弱）。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card
    from aistock_agent.services.rhythm_engine import POSITION_LADDER

    card, rows, win, _ = await _run_compose_full(
        "after_close",
        _bull_kline(),
        {"total_count": 100, "advance_count": 90},
        75,
        None,
    )
    assert card.evidence.stage_source == "computed"
    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["phase_evidence"]["stage_source"] == "computed"
    assert rhythm_card["position_source"] == "after_close_base"
    idx = rhythm_card["position_index"]
    assert idx is not None
    assert rhythm_card["position_band"]["text"] == "建议仓位：" + POSITION_LADDER[idx]
    # T8 接线：观察点已回填进账本，溯源文案从同一份账本派生
    from aistock_agent.services.rhythm_position_ledger import render_causality

    ledger = card.evidence.position_ledger
    assert ledger is not None
    assert ledger.watch_points
    assert rhythm_card["position_ledger"]["watch_points"] == [
        p.model_dump() for p in ledger.watch_points
    ]
    assert rhythm_card["causality_text"] == render_causality(ledger).model_dump()


@pytest.mark.asyncio
async def test_morning_inherits_basis_ledger_without_recompute():
    """继承路径沿用基准卡的序号/账本/观察点，不得用盘中 K 线重算（单一真相源）。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card
    from aistock_agent.services.rhythm_engine import derive_position_index
    from aistock_agent.services.rhythm_position_ledger import (
        build_watch_points,
        render_causality,
    )

    # 基准卡账本：空头闸门 → idx=0，且带 gate 观察点
    bear = [3400.0 - i * 5 for i in range(25)]
    base_idx, base_ledger = derive_position_index(
        index_closes=bear, mainline={"state": "unavailable"},
        event_d=None, event_result=None, breadth={"advance_ratio": 0.5},
    )
    base_ledger = base_ledger.model_copy(update={"watch_points": build_watch_points(
        base_ledger, mainline_facts={"state": "unavailable"}, event_d=None, closes=bear,
    )})
    assert base_idx == 0
    assert base_ledger.watch_points

    basis = {
        "content": {
            "basis_date": shanghai_today().isoformat(),
            "evidence": {"stage": "ebb", "stage_reason": "退潮"},
            "rhythm_card": {
                "position_index": base_idx,
                "position_ledger": base_ledger.model_dump(),
                "position_band": {"text": "基准卡文案"},
            },
        }
    }
    # 盘中 K 线是多头（本地重算会得 3 档）→ 断言沿用 0 档才证明「不重算」
    card, rows, win, _ = await _run_compose_full(
        "morning",
        _bull_kline(),
        {"total_count": 100, "advance_count": 90},
        75,
        basis,
    )
    assert card.evidence.stage_source == "inherited_basis"
    assert card.evidence.position is not None
    assert card.evidence.position.index == 0
    assert card.evidence.position_ledger is not None
    assert card.evidence.position_ledger.final_index == 0
    assert card.evidence.position_ledger.watch_points == base_ledger.watch_points

    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["position_index"] == 0
    assert rhythm_card["position_source"] == "inherited_basis"
    assert rhythm_card["position_ledger"]["watch_points"] == [
        p.model_dump() for p in base_ledger.watch_points
    ]
    assert rhythm_card["causality_text"] == render_causality(base_ledger).model_dump()


@pytest.mark.asyncio
async def test_morning_inherits_basis_when_local_stage_is_none():
    """D1：盘前本地判空时也应继承同日收盘基准（改前会直接留空）。"""
    basis = _prev_report("ebb", shanghai_today().isoformat())
    card = await _run_compose(
        "morning",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        basis,
    )
    assert card.evidence.stage == "ebb"
    assert card.evidence.stage_source == "inherited_basis"
    assert "沿用收盘判断" in card.evidence.stage_reason


@pytest.mark.asyncio
async def test_morning_still_prefers_basis_over_local_stage():
    """D1 不放宽语义：本地有方向时，同日档位仍以收盘基准为准（G2 契约）。"""
    basis = _prev_report("ebb", shanghai_today().isoformat())
    card = await _run_compose(
        "morning",
        _bull_kline(),
        {"total_count": 100, "advance_count": 90},
        75,
        basis,
    )
    assert card.evidence.stage == "ebb"
    assert card.evidence.stage_source == "inherited_basis"


@pytest.mark.asyncio
async def test_temperature_series_filled_from_archive(monkeypatch, tmp_path):
    """C1：temperature_series 不再是空数组，透出近 7 日温度（情绪强度维度）。"""
    import json

    from aistock_agent.agents.workers import rhythm_master

    (tmp_path / "2026-11-02.json").write_text(
        json.dumps({"date": "2026-11-02", "score": 81.5, "ice": {"consecutive_ice_days": 0}}),
        encoding="utf-8",
    )
    (tmp_path / "2026-11-01.json").write_text(
        json.dumps({"date": "2026-11-01", "score": 31.9, "ice": {"consecutive_ice_days": 0}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(rhythm_master, "sentiment_archive_dir", tmp_path)

    card, rows, win, _ = await _run_compose_full(
        "after_close", _bull_kline(), {"total_count": 100, "advance_count": 90}, 75, None
    )
    rhythm_card = rhythm_master._build_rhythm_card(card, win, rows)
    assert rhythm_card["temperature_series"] == [
        {"date": "2026-11-01", "score": 31.9},
        {"date": "2026-11-02", "score": 81.5},
    ]


@pytest.mark.asyncio
async def test_temperature_series_empty_when_archive_missing(monkeypatch, tmp_path):
    """归档缺失 → 空数组（前端 v-if 自然隐藏），不伪造占位。"""
    from aistock_agent.agents.workers import rhythm_master

    monkeypatch.setattr(rhythm_master, "sentiment_archive_dir", tmp_path / "nope")
    card, rows, win, _ = await _run_compose_full(
        "after_close", _bull_kline(), {"total_count": 100, "advance_count": 90}, 75, None
    )
    rhythm_card = rhythm_master._build_rhythm_card(card, win, rows)
    assert rhythm_card["temperature_series"] == []


@pytest.mark.asyncio
async def test_morning_inherits_basis_position_band():
    """档位继承自基准卡时，仓位也必须同源（不得用前一交易日 K 线重算）。

    旧基准卡无 `position_index` → 序号必须为 null（前端灰格），只回放继承文案，
    **不得按文案反推序号**（反推就是第二套算法）。
    """
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card

    basis = _prev_report_with_band("ebb", shanghai_today().isoformat(), "建议仓位：轻仓~观望")
    card, rows, win, _ = await _run_compose_full(
        "morning",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        basis,
    )
    assert card.evidence.stage_source == "inherited_basis"
    assert card.inherited_position_text == "建议仓位：轻仓~观望"
    # 旧基准卡无序号 → evidence.position 不可用（不猜）
    assert card.evidence.position is None
    assert card.evidence.position_ledger is None

    rhythm_card = _build_rhythm_card(card, win, rows)
    # 若走本地 derive_position_index（破位闸门）会得到「空仓观望」，与基准卡打架
    assert rhythm_card["position_band"]["text"] == "建议仓位：轻仓~观望"
    assert rhythm_card["position_index"] is None
    assert rhythm_card["position_ledger"] is None
    assert rhythm_card["position_source"] == "inherited_basis"


@pytest.mark.asyncio
async def test_morning_does_not_guess_index_when_basis_has_no_index():
    """基准卡缺 position_index（旧卡）→ 不重算、不猜，序号为 null（前端灰格）。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card

    basis = _prev_report("ebb", shanghai_today().isoformat())  # 无 rhythm_card
    card, rows, win, _ = await _run_compose_full(
        "morning",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        basis,
    )
    assert card.evidence.stage_source == "inherited_basis"
    assert card.inherited_position_text is None
    assert card.evidence.position is None
    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["position_index"] is None
    assert rhythm_card["position_source"] == "inherited_basis"
    assert rhythm_card["position_band"]["text"] == ""


def _write_archives(root, items: list[tuple[str, float]]) -> None:
    import json

    for date, score in items:
        (root / f"{date}.json").write_text(
            json.dumps({"date": date, "score": score, "ice": {"consecutive_ice_days": 0}}),
            encoding="utf-8",
        )


def test_load_sentiment_series_until_truncates_future(monkeypatch, tmp_path):
    """until 截断：只取截止日及以前的归档（回补历史卡防未来温度串味）。

    2026-11-13 诊断：归档目录按文件名取尾部，回补 11-11 卡会读到 11-12 的 52.2，
    比原卡更错。until 必须把未来归档排除在外。
    """
    from aistock_agent.agents.workers import rhythm_master

    _write_archives(
        tmp_path,
        [("2026-11-10", 89.0), ("2026-11-11", 83.8), ("2026-11-12", 52.2)],
    )
    monkeypatch.setattr(rhythm_master, "sentiment_archive_dir", tmp_path)

    series, scores, _, _ = rhythm_master._load_sentiment_series(days=7, until="2026-11-11")
    assert [x["date"] for x in series] == ["2026-11-10", "2026-11-11"]
    assert scores[-1] == 83.8


def test_load_sentiment_series_until_before_all_returns_empty(monkeypatch, tmp_path):
    """until 早于全部归档 → 空序列，不伪造占位。"""
    from aistock_agent.agents.workers import rhythm_master

    _write_archives(tmp_path, [("2026-11-10", 89.0), ("2026-11-11", 83.8)])
    monkeypatch.setattr(rhythm_master, "sentiment_archive_dir", tmp_path)

    series, scores, _, _ = rhythm_master._load_sentiment_series(days=7, until="2026-11-01")
    assert series == []
    assert scores == []


def test_load_sentiment_series_without_until_keeps_latest(monkeypatch, tmp_path):
    """不传 until 保持旧行为（调度路径不受影响）。"""
    from aistock_agent.agents.workers import rhythm_master

    _write_archives(
        tmp_path,
        [("2026-11-10", 89.0), ("2026-11-11", 83.8), ("2026-11-12", 52.2)],
    )
    monkeypatch.setattr(rhythm_master, "sentiment_archive_dir", tmp_path)

    series, _, _, _ = rhythm_master._load_sentiment_series(days=2)
    assert [x["date"] for x in series] == ["2026-11-11", "2026-11-12"]
