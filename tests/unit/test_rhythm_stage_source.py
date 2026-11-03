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
        return await _compose_card(shanghai_today().isoformat(), slot)


async def _run_compose(slot: str, kline, breadth, fg, rhythm_report):
    card, _, _ = await _run_compose_full(slot, kline, breadth, fg, rhythm_report)
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
    prev = _prev_report("ebb", "2026-10-30")
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
async def test_position_band_downgraded_when_inherited_prev_day():
    """问题 3：中性沿用来的档位无独立空头证据，仓位不套用「指数破位 → 空仓」口径。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card

    prev = _prev_report("ebb", "2026-10-30")
    card, rows, win = await _run_compose_full(
        "after_close",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        prev,
    )
    assert card.evidence.stage_source == "inherited_prev_day"
    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["phase_evidence"]["stage_source"] == "inherited_prev_day"
    # ebb 档 evidence.position = 轻仓~观望；不应被破位闸门压成「空仓观望」
    assert "轻仓" in rhythm_card["position_band"]["text"]
    assert "空仓" not in rhythm_card["position_band"]["text"]


@pytest.mark.asyncio
async def test_position_band_keeps_gate_when_stage_computed():
    """硬判场景仍走 derive_position_text（破位闸门语义不被削弱）。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card

    card, rows, win = await _run_compose_full(
        "after_close",
        _bull_kline(),
        {"total_count": 100, "advance_count": 90},
        75,
        None,
    )
    assert card.evidence.stage_source == "computed"
    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["phase_evidence"]["stage_source"] == "computed"
    assert rhythm_card["position_band"]["text"].startswith("建议仓位：")


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
    assert "沿用收盘基准" in card.evidence.stage_reason


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

    card, rows, win = await _run_compose_full(
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
    card, rows, win = await _run_compose_full(
        "after_close", _bull_kline(), {"total_count": 100, "advance_count": 90}, 75, None
    )
    rhythm_card = rhythm_master._build_rhythm_card(card, win, rows)
    assert rhythm_card["temperature_series"] == []


@pytest.mark.asyncio
async def test_morning_inherits_basis_position_band():
    """档位继承自基准卡时，仓位也必须同源（不得用前一交易日 K 线重算）。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card

    basis = _prev_report_with_band("ebb", shanghai_today().isoformat(), "建议仓位：轻仓~观望")
    card, rows, win = await _run_compose_full(
        "morning",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        basis,
    )
    assert card.evidence.stage_source == "inherited_basis"
    assert card.inherited_position_text == "建议仓位：轻仓~观望"

    rhythm_card = _build_rhythm_card(card, win, rows)
    # 若走本地 derive_position_text（破位闸门）会得到「空仓观望」，与基准卡打架
    assert rhythm_card["position_band"]["text"] == "建议仓位：轻仓~观望"


@pytest.mark.asyncio
async def test_morning_falls_back_to_local_position_when_basis_has_no_band():
    """基准卡缺 position_band（旧卡）→ 回退本地计算，不伪造。"""
    from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card

    basis = _prev_report("ebb", shanghai_today().isoformat())  # 无 rhythm_card
    card, rows, win = await _run_compose_full(
        "morning",
        _mild_kline(),
        {"total_count": 100, "advance_count": 50},
        45,
        basis,
    )
    assert card.evidence.stage_source == "inherited_basis"
    assert card.inherited_position_text is None
    rhythm_card = _build_rhythm_card(card, win, rows)
    assert rhythm_card["position_band"]["text"].startswith("建议仓位：")
