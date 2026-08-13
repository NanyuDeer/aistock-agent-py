from typing import Any

import pytest

from aistock_agent.services.event_scoring_llm import (
    DeepScoreOutput,
    QuickFilterItem,
    QuickFilterOutput,
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
