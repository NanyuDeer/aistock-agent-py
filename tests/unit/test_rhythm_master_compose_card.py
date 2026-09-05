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
        "aistock_agent.agents.workers.rhythm_master.node_api.get_last_close_snapshot",
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
        "aistock_agent.agents.workers.rhythm_master.node_api.get_last_close_snapshot",
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

