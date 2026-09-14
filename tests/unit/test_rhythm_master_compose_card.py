from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.utils.date import shanghai_today


def _mock_kline(n_rows: int):
    rows = []
    for i in range(n_rows):
        close = 3000.0 + i * 10
        rows.append({
            "trade_date": "20260901",
            "open": close - 5,
            "high": close + 10,
            "low": close - 10,
            "close": close,
            "pct_chg": 0.5,
            "amount": 100.0 + i,
        })
    return rows


@pytest.mark.asyncio
async def test_compose_card_short_kline_forces_stage_none_and_missing():
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    basis = shanghai_today().isoformat()
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=_mock_kline(5)),  # <20 行
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
    ) as vs:
        vs.return_value = True
        card, _, _ = await _compose_card(basis, "after_close")
    assert card.evidence.stage is None
    assert "指数K线不足" in card.evidence.data_missing


@pytest.mark.asyncio
async def test_compose_card_passes_historical_kline_params():
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    basis = shanghai_today().isoformat()
    kline_mock = AsyncMock(return_value=_mock_kline(200))
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        kline_mock,
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
    ) as vs:
        vs.return_value = True
        await _compose_card(basis, "after_close")
    _, kwargs = kline_mock.call_args
    assert kwargs["days"] == 200
    assert "start_date" not in kwargs
    assert kwargs["end_date"] == basis.replace("-", "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (8.0e8, 8000.0),  # Tushare 千元 → 亿元：8e8 千元 = 8000 亿
        (2.0e8, 2000.0),
        (0.0, 0.0),
        (None, 0.0),  # 缺失如实转 0（量能仅 ratio/均量阈值，0 不伪造）
    ],
)
def test_amount_yi_converts_qian_yuan_to_yi(raw, expected):
    from aistock_agent.agents.workers.rhythm_master import _amount_yi

    assert _amount_yi(raw) == pytest.approx(expected)


def _mock_kline_dated(n_rows: int, last_trade_date: str | None):
    rows = _mock_kline(n_rows)
    for r in rows:
        r["trade_date"] = "20260801"
    if rows and last_trade_date is not None:
        rows[-1]["trade_date"] = last_trade_date
    return rows


async def _compose(slot: str, basis: str, kline_value):
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=kline_value),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": []})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
    ) as vs:
        vs.return_value = True
        card, rows, _ = await _compose_card(basis, slot)
    return card


_GATE_MSG = "基准日无当日K线"


@pytest.mark.asyncio
async def test_after_close_gate_when_last_row_not_basis():
    basis = "2026-09-10"
    card = await _compose("after_close", basis, _mock_kline_dated(200, "20260909"))
    assert card.evidence.stage is None
    assert any(_GATE_MSG in m for m in card.evidence.data_missing)


@pytest.mark.asyncio
async def test_after_close_no_gate_when_last_row_equals_basis():
    basis = "2026-09-10"
    card = await _compose("after_close", basis, _mock_kline_dated(200, "20260910"))
    assert not any(_GATE_MSG in m for m in card.evidence.data_missing)


@pytest.mark.asyncio
async def test_morning_not_gated_when_last_row_older_than_basis():
    basis = "2026-09-10"
    card = await _compose("morning", basis, _mock_kline_dated(200, "20260909"))
    assert not any(_GATE_MSG in m for m in card.evidence.data_missing)


@pytest.mark.asyncio
async def test_empty_kline_short_circuits_without_exception():
    basis = "2026-09-10"
    card = await _compose("after_close", basis, [])
    assert card.evidence.stage is None
    assert any("指数K线不足" in m for m in card.evidence.data_missing)


def test_event_confirm_requires_high_importance():
    from aistock_agent.agents.workers.rhythm_master import _event_confirm

    # medium 事件带 result：不得抬确认
    assert _event_confirm([{"importance": "medium", "result": "超预期"}]) is False
    # low 事件带 result：不得抬确认
    assert _event_confirm([{"importance": "low", "result": "不及预期"}]) is False
    # high 事件带 result：确认
    assert _event_confirm([{"importance": "high", "result": "超预期"}]) is True
    # high 事件但 result 非法 / 缺失：不确认
    assert _event_confirm([{"importance": "high", "result": "符合"}]) is False
    assert _event_confirm([{"importance": "high"}]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_confirm"),
    [
        ([{"importance": "medium", "result": "超预期"}], False),
        ([{"importance": "high", "result": "超预期"}], True),
    ],
)
async def test_compose_card_feeds_event_confirm_into_detect_certainty(events, expected_confirm):
    from aistock_agent.agents.workers import rhythm_master as worker_mod

    basis = "2026-09-10"
    captured: list[bool] = []

    def _spy(**kwargs):
        captured.append(kwargs["event_confirm"])
        return ("low", "spy")

    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=_mock_kline_dated(200, "20260910")),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 40}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 50}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {
            "events": events,
            "high_events": [e for e in events if e.get("importance") == "high"],
            "source_missing": False,
        })()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.validate_synthesis",
        return_value=False,
    ), patch.object(worker_mod.ev, "detect_certainty", _spy):
        await worker_mod._compose_card(basis, "after_close")

    assert captured == [expected_confirm]


@pytest.mark.asyncio
async def test_breadth_snapshot_uses_kline_last_date():
    from unittest.mock import AsyncMock, patch

    from aistock_agent.agents.workers import rhythm_master as worker_mod
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    kline = _mock_kline_dated(200, "20260911")  # 末日 = 20260911
    snap = AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 60}})
    win_stub = type("W", (), {"events": [], "high_events": [], "source_missing": False})()
    with (
        patch.object(worker_mod.node_api, "get_index_kline", AsyncMock(return_value=kline)),
        patch.object(worker_mod.node_api, "get_close_snapshot", snap),
        patch.object(worker_mod.node_api, "get_fear_greed", AsyncMock(return_value={"index": 40})),
        patch.object(worker_mod, "load_event_window", AsyncMock(return_value=win_stub)),
        patch.object(worker_mod, "run_synthesis", AsyncMock(return_value=None)),
        patch.object(worker_mod, "validate_synthesis", return_value=False),
    ):
        await _compose_card("2026-09-14", "morning")

    snap.assert_awaited_once()
    # 关键：以 K 线末日（证据日）而非「严格早于今天」取快照
    assert snap.await_args.args[0] == "20260911"
