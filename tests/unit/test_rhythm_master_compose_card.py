from unittest.mock import patch

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
    with patch("aistock_agent.agents.workers.rhythm_master.node_api") as api, \
         patch("aistock_agent.agents.workers.rhythm_master.load_event_window") as lew, \
         patch("aistock_agent.agents.workers.rhythm_master.run_synthesis") as rs, \
         patch("aistock_agent.agents.workers.rhythm_master.validate_synthesis") as vs:
        api.get_index_kline.return_value = _mock_kline(5)  # <20 行
        api.get_fear_greed.return_value = {"index": 40}
        api.get_last_close_snapshot.return_value = {"breadth": {"total_count": 100, "advance_count": 50}}
        lew.return_value = type("W", (), {"events": [], "high_events": []})()
        vs.return_value = True
        rs.return_value = None
        card = await _compose_card(basis, "after_close")
    assert card.evidence.stage is None
    assert "指数K线不足" in card.evidence.data_missing


@pytest.mark.asyncio
async def test_compose_card_passes_historical_kline_params():
    from aistock_agent.agents.workers.rhythm_master import _compose_card

    basis = shanghai_today().isoformat()
    with patch("aistock_agent.agents.workers.rhythm_master.node_api") as api, \
         patch("aistock_agent.agents.workers.rhythm_master.load_event_window") as lew, \
         patch("aistock_agent.agents.workers.rhythm_master.run_synthesis") as rs, \
         patch("aistock_agent.agents.workers.rhythm_master.validate_synthesis") as vs:
        api.get_index_kline.return_value = _mock_kline(200)
        api.get_fear_greed.return_value = {"index": 40}
        api.get_last_close_snapshot.return_value = {"breadth": {"total_count": 100, "advance_count": 50}}
        lew.return_value = type("W", (), {"events": [], "high_events": []})()
        vs.return_value = True
        rs.return_value = None
        await _compose_card(basis, "after_close")
    _, kwargs = api.get_index_kline.call_args
    assert kwargs["days"] == 200
    assert "start_date" not in kwargs
    assert kwargs["end_date"] == basis.replace("-", "")