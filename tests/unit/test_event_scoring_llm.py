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
    _quick_filter,
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


class _FakeQuickRunnable:
    def __init__(self, output: QuickFilterOutput) -> None:
        self._output = output

    async def ainvoke(self, payload: dict[str, Any]) -> QuickFilterOutput:
        self.last_payload = payload
        return self._output


class _KeepAllQuickRunnable(_FakeQuickRunnable):
    """按 ainvoke 收到的批次动态响应：全部 keep（分批用例专用）。

    真实契约下 with_chat_structured_output 收到 (llm, schema) 两个参数，
    批次 payload 只在 ainvoke 时可见，故不能在构造期生成输出。
    """

    def __init__(self) -> None:
        super().__init__(QuickFilterOutput(items=[]))

    async def ainvoke(self, payload: dict[str, Any]) -> QuickFilterOutput:
        self.last_payload = payload
        return QuickFilterOutput(items=[
            QuickFilterItem(event_id=item["event_id"], keep=True) for item in payload["events"]
        ])


@pytest.mark.asyncio
async def test_quick_filter_keeps_only_marked():
    ev1 = _ev("e1", "aaa")
    ev2 = _ev("e2", "bbb")
    fake = _FakeQuickRunnable(QuickFilterOutput(items=[
        QuickFilterItem(event_id="e1", keep=True),
        QuickFilterItem(event_id="e2", keep=False),
    ]))
    with patch("aistock_agent.services.event_scoring_llm.get_quick_think"), \
            patch("aistock_agent.services.event_scoring_llm.with_chat_structured_output", return_value=fake):
        result = await _quick_filter([ev1, ev2])
    assert result == {"aaa"}


@pytest.mark.asyncio
async def test_quick_filter_failure_keeps_all():
    ev1 = _ev("e1", "aaa")
    with patch("aistock_agent.services.event_scoring_llm.get_quick_think"), \
            patch("aistock_agent.services.event_scoring_llm.with_chat_structured_output", side_effect=RuntimeError("boom")):
        result = await _quick_filter([ev1])
    assert result == {"aaa"}


@pytest.mark.asyncio
async def test_quick_filter_batches_by_config():
    events = [_ev(f"e{i}", f"h{i:03d}") for i in range(5)]
    runnables: list[_KeepAllQuickRunnable] = []
    with patch("aistock_agent.services.event_scoring_llm.get_quick_think"), \
            patch("aistock_agent.services.event_scoring_llm.with_chat_structured_output") as mock_wrap:
        def fake_runnable(*_args: Any) -> Any:
            fake = _KeepAllQuickRunnable()
            runnables.append(fake)
            return fake
        mock_wrap.side_effect = fake_runnable
        with patch("aistock_agent.config.settings.event_scoring_quick_batch_size", 2):
            result = await _quick_filter(events)
    assert [len(r.last_payload["events"]) for r in runnables] == [2, 2, 1]  # 5 条按每批 2 分 3 批
    assert len(result) == 5
