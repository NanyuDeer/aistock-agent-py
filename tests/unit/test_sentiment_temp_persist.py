"""情绪温度落盘/加载/晨报上下文测试。"""
import json

import pytest

from aistock_agent.services.sentiment_temp import (
    build_morning_sentiment_context,
    build_sentiment_payload,
    load_latest_sentiment,
    load_previous_archive,
    persist_sentiment,
)


def _ice_payload() -> dict[str, object]:
    return build_sentiment_payload(
        date="2026-08-22",
        score=18.0,
        level="冰点",
        metrics={
            "up_count": 12, "down_count": 96, "broken_count": 40,
            "highest_board": 2, "advance_ratio": 0.21, "main_force_net_yi": -128.5,
        },
        ice={"is_ice": True, "consecutive_ice_days": 2, "is_extreme_ice": True},
        prediction={"generated": True, "text": "昨日情绪冰点，短期修复概率较高，关注超跌方向。"},
    )


@pytest.mark.asyncio
async def test_persist_and_load_roundtrip(tmp_path) -> None:
    payload = _ice_payload()
    persist_sentiment(payload, str(tmp_path))
    (tmp_path / "2026-08-22.json").read_text(encoding="utf-8")  # 存在
    latest = await load_latest_sentiment(str(tmp_path))
    assert latest is not None
    assert latest["date"] == "2026-08-22"
    assert latest["score"] == 18.0
    assert latest["ice"]["is_extreme_ice"] is True


@pytest.mark.asyncio
async def test_load_latest_missing_dir(tmp_path) -> None:
    assert await load_latest_sentiment(str(tmp_path / "nonexistent")) is None


def test_load_previous_placeholder(tmp_path) -> None:
    # 先写 08-21（阈值内）再写 08-22，读 08-22 的 previous 应返回 08-21
    for d, score in (("2026-08-20", 30.0), ("2026-08-21", 15.0)):
        payload = build_sentiment_payload(d, score, "低迷", {}, {"is_ice": False}, {})
        persist_sentiment(payload, str(tmp_path))
    prev = load_previous_archive(str(tmp_path), "2026-08-22")
    assert prev is not None and prev["date"] == "2026-08-21"


@pytest.mark.asyncio
async def test_build_morning_context_ice(tmp_path) -> None:
    payload = _ice_payload()
    persist_sentiment(payload, str(tmp_path))
    ctx = build_morning_sentiment_context(
        await load_latest_sentiment(str(tmp_path)), extreme_days=2
    )
    assert "冰点" in ctx
    assert "连续2日" in ctx
    assert "短期修复概率较高" in ctx
    assert "涨停12" in ctx


def test_build_morning_context_normal() -> None:
    payload = build_sentiment_payload(
        "2026-08-21", 52.0, "常温", {}, {"is_ice": False}, {}
    )
    ctx = build_morning_sentiment_context(payload, extreme_days=2)
    assert ctx == "昨日（2026-08-21）短线情绪温度 52（常温）。"


def test_build_morning_context_none() -> None:
    assert build_morning_sentiment_context(None, extreme_days=2) == ""
