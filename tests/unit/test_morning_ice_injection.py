"""晨报情绪温度注入 wiring 测试：三档注入 + 无文件不注入。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import morning as morning_mod
from aistock_agent.prompts.workers.morning import SENTIMENT_ICE_CONTEXT_PLACEHOLDER

_ICE_LATEST = {
    "date": "2026-08-22",
    "score": 18.0,
    "level": "冰点",
    "ice": {"is_ice": True, "consecutive_ice_days": 2, "is_extreme_ice": True},
    "metrics": {"up_count": 12, "down_count": 96, "broken_count": 40,
                 "highest_board": 2, "advance_ratio": 0.21, "main_force_net_yi": -128.5},
    "prediction": {"generated": True, "text": "冰点次日修复概率较高，关注超跌方向。"},
}

_NORMAL_LATEST = {
    "date": "2026-08-21", "score": 52.0, "level": "常温",
    "ice": {"is_ice": False, "consecutive_ice_days": 0, "is_extreme_ice": False},
    "metrics": {}, "prediction": {},
}


def _dummy_report() -> dict[str, object]:
    return {
        "display_report": {"summary": "s", "details": "d" * 300, "stocks": [], "risks": []},
        "podcast_brief": "p" * 160,
        "schema_version": "2.0",
    }


@pytest.mark.asyncio
async def test_morning_inject_ice_prediction() -> None:
    captured: dict[str, str] = {}

    async def _fake_invoke(system_prompt: str) -> dict[str, object]:
        captured["prompt"] = system_prompt
        return _dummy_report()

    with (
        patch.object(morning_mod, "get_cached_briefing", AsyncMock(return_value=None)),
        patch.object(morning_mod, "_invoke_morning_agent", side_effect=_fake_invoke),
        patch.object(morning_mod, "persist_morning_report", AsyncMock(return_value=True)),
        patch.object(morning_mod, "archive_morning", lambda _: None),
        patch.object(
            morning_mod, "load_latest_sentiment",
            AsyncMock(return_value=_ICE_LATEST),
        ),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            AsyncMock(return_value=[]),
        ),
    ):
        await morning_mod.run({"report_date": "2026-08-25"})

    prompt = captured["prompt"]
    assert SENTIMENT_ICE_CONTEXT_PLACEHOLDER not in prompt
    assert "短线情绪冰点" in prompt
    assert "冰点次日修复概率较高" in prompt
    assert "连续2日冰点" in prompt


@pytest.mark.asyncio
async def test_morning_inject_normal_overview() -> None:
    captured: dict[str, str] = {}

    async def _fake_invoke(system_prompt: str) -> dict[str, object]:
        captured["prompt"] = system_prompt
        return _dummy_report()

    with (
        patch.object(morning_mod, "get_cached_briefing", AsyncMock(return_value=None)),
        patch.object(morning_mod, "_invoke_morning_agent", side_effect=_fake_invoke),
        patch.object(morning_mod, "persist_morning_report", AsyncMock(return_value=True)),
        patch.object(morning_mod, "archive_morning", lambda _: None),
        patch.object(
            morning_mod, "load_latest_sentiment",
            AsyncMock(return_value=_NORMAL_LATEST),
        ),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            AsyncMock(return_value=[]),
        ),
    ):
        await morning_mod.run({"report_date": "2026-08-25"})

    assert "短线情绪温度 52（常温）" in captured["prompt"]


@pytest.mark.asyncio
async def test_morning_no_file_no_injection() -> None:
    captured: dict[str, str] = {}

    async def _fake_invoke(system_prompt: str) -> dict[str, object]:
        captured["prompt"] = system_prompt
        return _dummy_report()

    with (
        patch.object(morning_mod, "get_cached_briefing", AsyncMock(return_value=None)),
        patch.object(morning_mod, "_invoke_morning_agent", side_effect=_fake_invoke),
        patch.object(morning_mod, "persist_morning_report", AsyncMock(return_value=True)),
        patch.object(morning_mod, "archive_morning", lambda _: None),
        patch.object(morning_mod, "load_latest_sentiment", AsyncMock(return_value=None)),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            AsyncMock(return_value=[]),
        ),
    ):
        await morning_mod.run({"report_date": "2026-08-25"})

    prompt = captured["prompt"]
    assert SENTIMENT_ICE_CONTEXT_PLACEHOLDER not in prompt
    assert "短线情绪温度" not in prompt
