import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_scoring_llm import (
    DeepScoreOutput,
    QuickFilterItem,
    QuickFilterOutput,
    _cache_get,
    _cache_set,
    apply_llm_scores,
)


def _ev(event_id: str, content_hash: str, impact: int = 3) -> dict[str, Any]:
    return {
        "event_id": event_id, "title": f"事件{event_id}", "summary": "s",
        "url": f"https://example.com/{event_id}", "impact_score": impact,
        "direction": "neutral", "involved_keywords": [], "source": "cls",
        "source_level": "A", "content_hash": content_hash,
        "scrape_at": "2026-08-13 10:00:00", "score_date": "2026-08-13",
        "payload": {},
    }


def test_apply_llm_scores_overrides_rule_score():
    ev = _ev("e1", "aaa")
    result = apply_llm_scores([ev], {"aaa": DeepScoreOutput(impact_score=5, direction="positive")})
    assert result[0]["impact_score"] == 5
    assert result[0]["direction"] == "positive"


def test_apply_llm_scores_clamps_out_of_range():
    ev = _ev("e1", "aaa")
    result = apply_llm_scores([ev], {"aaa": DeepScoreOutput(impact_score=9, direction="positive")})
    assert result[0]["impact_score"] == 5
    result2 = apply_llm_scores([ev], {"aaa": DeepScoreOutput(impact_score=-1, direction="positive")})
    assert result2[0]["impact_score"] == 1


def test_apply_llm_scores_keeps_original_on_missing_or_invalid():
    ev = _ev("e1", "aaa")
    result = apply_llm_scores([ev], {})  # 无评分 → 保持原值
    assert result[0]["impact_score"] == 3
    result2 = apply_llm_scores(
        # DeepScoreOutput.direction 是 Literal，非法值无法经常规构造器传入；
        # 用 model_construct 跳过 Pydantic 校验以覆盖 apply_llm_scores 的防御分支
        [ev],
        {"aaa": DeepScoreOutput.model_construct(impact_score=5, direction="invalid")},
    )
    assert result2[0]["direction"] == "neutral"  # direction 非法 → 保留原值


@pytest.mark.asyncio
async def test_cache_get_hit_parses_score():
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=json.dumps(
        {"impact_score": 5, "direction": "positive", "reason": "r"}
    ).encode())
    with patch("aistock_agent.services.event_scoring_llm.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=fake_client)
        result = await _cache_get("aaa")
    assert result is not None
    assert result.impact_score == 5
    assert result.direction == "positive"


@pytest.mark.asyncio
async def test_cache_get_miss_returns_none():
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=None)
    with patch("aistock_agent.services.event_scoring_llm.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=fake_client)
        result = await _cache_get("aaa")
    assert result is None


@pytest.mark.asyncio
async def test_cache_set_writes_with_ttl():
    fake_client = AsyncMock()
    with patch("aistock_agent.services.event_scoring_llm.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=fake_client)
        await _cache_set("aaa", DeepScoreOutput(impact_score=4, direction="negative"))
    args, kwargs = fake_client.setex.await_args
    assert args[0] == "event_score:aaa"
    assert args[1] == 86400
    assert json.loads(args[2])["impact_score"] == 4
