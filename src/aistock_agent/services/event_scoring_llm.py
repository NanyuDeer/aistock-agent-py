"""事件抓取中台 LLM 评分 — Phase-2（quick 粗筛 + deep 精评）。

在 Phase-1 规则评分（event_scoring.py）之上叠加 LLM 精评：
- 候选门槛：仅规则评分 >= EVENT_SCORING_CANDIDATE_THRESHOLD 的事件送 LLM
- 缓存：content_hash -> {impact_score, direction}，TTL 24h，防盘中重复计费
- 降级：任一阶段异常 -> 保持规则评分（返回原列表），不阻断抓取
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from aistock_agent.config import settings
from aistock_agent.services import event_store
from aistock_agent.services.llm import get_deep_think, get_quick_think, with_chat_structured_output
from aistock_agent.services.redis_pool import RedisPool

logger = structlog.get_logger()

_CACHE_PREFIX = "event_score:"


class QuickFilterItem(BaseModel):
    """quick_think 粗筛单条输出。event_id 原样回传用于关联；keep 为是否继续精评。"""

    event_id: str
    keep: bool


class QuickFilterOutput(BaseModel):
    items: list[QuickFilterItem]


class DeepScoreOutput(BaseModel):
    """deep_think 精评输出。impact_score 1-5 重大度；direction 事件方向。"""

    impact_score: int
    direction: Literal["positive", "negative", "neutral"]
    reason: str = ""


def apply_llm_scores(
    events: list[event_store.EventRecord],
    scores: dict[str, DeepScoreOutput],
) -> list[event_store.EventRecord]:
    """把 LLM 评分合并进事件列表（按 content_hash 覆盖 impact_score/direction）。

    分数截断到 [1,5]；direction 非法时保留原值；未评事件保持原值。
    纯函数，无 IO。
    """
    result: list[event_store.EventRecord] = []
    for ev in events:
        score = scores.get(ev["content_hash"])
        if score is None:
            result.append(ev)
            continue
        impact = max(1, min(5, int(score.impact_score)))
        direction = (
            score.direction
            if score.direction in ("positive", "negative", "neutral")
            else ev["direction"]
        )
        result.append({**ev, "impact_score": impact, "direction": direction})
    return result


async def _cache_get(content_hash: str) -> DeepScoreOutput | None:
    """读评分缓存（event_score:{hash}）。未命中/异常返回 None。"""
    try:
        client = await RedisPool.get_client()
        cached = await client.get(f"{_CACHE_PREFIX}{content_hash}")
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            return DeepScoreOutput(**parsed)
    except Exception:  # noqa: BLE001
        logger.debug("event_score_cache_get_failed", hash=content_hash[:16])
    return None


async def _cache_set(content_hash: str, score: DeepScoreOutput) -> None:
    """写评分缓存（TTL 由配置控制）。失败仅 warning。"""
    try:
        client = await RedisPool.get_client()
        await client.setex(
            f"{_CACHE_PREFIX}{content_hash}",
            settings.event_scoring_cache_ttl,
            json.dumps(score.model_dump(), ensure_ascii=False),
        )
    except Exception:  # noqa: BLE001
        logger.debug("event_score_cache_set_failed", hash=content_hash[:16])


async def _quick_filter(events: list[event_store.EventRecord]) -> set[str]:
    """quick_think 批量粗筛：返回应继续精评的 content_hash 集合。

    按 EVENT_SCORING_QUICK_BATCH_SIZE 分批（1 次调用评 N 条）；异常时保守
    返回全部（交 deep 单条兜底判定，避免误滤掉重大事件）。
    """
    keep: set[str] = set()
    batch_size = max(1, settings.event_scoring_quick_batch_size)
    for start in range(0, len(events), batch_size):
        batch = events[start : start + batch_size]
        payload = [
            {"event_id": ev["event_id"], "title": ev["title"], "summary": ev["summary"]}
            for ev in batch
        ]
        try:
            llm = with_chat_structured_output(get_quick_think(), QuickFilterOutput)
            output = await llm.ainvoke({"events": payload})
            keep_flags = {item.event_id: item.keep for item in output.items}
            kept = [ev for ev in batch if keep_flags.get(ev["event_id"], False)]
            keep |= {ev["content_hash"] for ev in kept}
            logger.info("event_score_quick_filter", batch=len(batch), kept=len(kept))
        except Exception:  # noqa: BLE001
            logger.warning("event_score_quick_filter_failed", batch=len(batch))
            keep |= {ev["content_hash"] for ev in batch}
    return keep
