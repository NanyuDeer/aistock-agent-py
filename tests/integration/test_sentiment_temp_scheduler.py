"""收盘编排任务测试：mock close-snapshot + normalize，验证落盘与脱敏。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.sentiment_temp import compute_and_persist_sentiment_temp


@pytest.mark.asyncio
async def test_compute_and_persist_happy_path(tmp_path, monkeypatch) -> None:
    close_data = {
        "status": "complete",
        "trade_date": "2026-08-22",
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
async def test_compute_skip_on_stale_trade_date(tmp_path, monkeypatch) -> None:
    """Node 返回上一交易日 complete 数据（trade_date ≠ report_date）→ 跳过且不落盘。

    防呆：今日数据未就绪时 Node 可能返回前一交易日的 complete 快照；若把它按
    今日日期落盘会污染连冰计数与次日晨报引用（C2 保护的 skip 语义版本）。
    """
    close_data = {
        "status": "complete",
        "trade_date": "2026-08-21",
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

    assert payload is None
    assert not (tmp_path / "latest.json").exists()
    assert not (tmp_path / "2026-08-22.json").exists()


@pytest.mark.asyncio
async def test_compute_non_ice_no_llm_prediction(tmp_path, monkeypatch) -> None:
    """非冰点（score > 20）：不调用 LLM 预判，prediction 仅 {generated: False}。"""
    close_data = {
        "status": "complete",
        "trade_date": "2026-08-22",
        "indexes": [{"name": "上证指数", "code": "000001.SH", "close": 3100.0, "change_pct": 0.8}],
        "breadth": {"advance_ratio": 0.7},
        "limits": {"up_count": 60, "down_count": 10, "broken_count": 5, "highest_board": 4},
        "turnover": {"amount_yi": 8000.0},
        "main_force": {"large_and_extra_large_net_yuan": 80e8},
    }
    with (
        patch(
            "aistock_agent.services.sentiment_temp.node_api",
            AsyncMock(get=AsyncMock(return_value=close_data)),
        ),
        patch(
            "aistock_agent.services.sentiment_temp.generate_ice_prediction",
            new=AsyncMock(),
        ) as mock_llm,
        patch(
            "aistock_agent.services.sentiment_temp.is_trading_day",
            return_value=True,
        ),
    ):
        payload = await compute_and_persist_sentiment_temp("2026-08-22", output_dir=str(tmp_path))

    assert payload is not None
    assert payload["score"] > 20
    assert payload["ice"]["is_ice"] is False
    assert payload["prediction"] == {"generated": False}
    assert "text" not in payload["prediction"]
    mock_llm.assert_not_awaited()
    assert (tmp_path / "latest.json").exists()
    assert (tmp_path / "2026-08-22.json").exists()


@pytest.mark.asyncio
async def test_compute_ice_two_consecutive_days_extreme(tmp_path, monkeypatch) -> None:
    """前一日冰点归档 + 今日冰点 → 连冰 2 日并升级极端冰点（核心分支集成覆盖）。"""
    from aistock_agent.services.sentiment_temp import (
        build_sentiment_payload,
        persist_sentiment,
    )

    persist_sentiment(
        build_sentiment_payload(
            "2026-08-21",
            15.0,
            "冰点",
            {
                "up_count": 4,
                "down_count": 98,
                "broken_count": 40,
                "broken_ratio": 0.91,
                "highest_board": 1,
                "advance_ratio": 0.05,
                "main_force_net_yi": -150.0,
            },
            {"is_ice": True, "consecutive_ice_days": 1, "is_extreme_ice": False},
            {"generated": False},
        ),
        str(tmp_path),
    )

    close_data = {
        "status": "complete",
        "trade_date": "2026-08-22",
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
    assert payload["ice"]["is_ice"] is True
    assert payload["ice"]["consecutive_ice_days"] == 2
    assert payload["ice"]["is_extreme_ice"] is True


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
