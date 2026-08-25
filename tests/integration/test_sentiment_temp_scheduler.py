"""收盘编排任务测试：mock close-snapshot + normalize，验证落盘与脱敏。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.sentiment_temp import compute_and_persist_sentiment_temp


@pytest.mark.asyncio
async def test_compute_and_persist_happy_path(tmp_path, monkeypatch) -> None:
    close_data = {
        "status": "complete",
        "indexes": [{"name": "上证指数", "code": "000001.SH", "close": 3000.0, "change_pct": -1.2}],
        "breadth": {"advance_ratio": 0.21},
        "limits": {"up_count": 5, "down_count": 96, "broken_count": 40, "highest_board": 2},
        "turnover": {"amount_yi": 8000.0},
        "main_force": {"large_and_extra_large_net_yuan": -128.5e8},
    }
    with (
        patch(
            "aistock_agent.services.sentiment_temp.node_api",
            AsyncMock(get=AsyncMock(return_value=close_data)),
        ),
        patch(
            "aistock_agent.services.sentiment_temp.generate_ice_prediction",
            AsyncMock(return_value=(True, "冰点次日修复概率较高。")),
        ),
        patch(
            "aistock_agent.services.sentiment_temp.is_trading_day",
            return_value=True,
        ),
    ):
        payload = await compute_and_persist_sentiment_temp("2026-08-22", output_dir=str(tmp_path))

    assert payload is not None
    assert payload["score"] < 20
    assert payload["ice"]["is_ice"] is True
    assert payload["prediction"]["text"] == "冰点次日修复概率较高。"
    assert (tmp_path / "latest.json").exists()
    assert (tmp_path / "2026-08-22.json").exists()


@pytest.mark.asyncio
async def test_compute_skip_on_missing_snapshot(tmp_path, monkeypatch) -> None:
    with (
        patch(
            "aistock_agent.services.sentiment_temp.node_api",
            AsyncMock(get=AsyncMock(return_value=None)),
        ),
        patch(
            "aistock_agent.services.sentiment_temp.is_trading_day",
            return_value=True,
        ),
    ):
        payload = await compute_and_persist_sentiment_temp("2026-08-22", output_dir=str(tmp_path))
    assert payload is None


@pytest.mark.asyncio
async def test_compute_skip_on_non_trading_day(tmp_path, monkeypatch) -> None:
    with patch("aistock_agent.services.sentiment_temp.is_trading_day", return_value=False):
        payload = await compute_and_persist_sentiment_temp("2026-08-23", output_dir=str(tmp_path))
    assert payload is None
