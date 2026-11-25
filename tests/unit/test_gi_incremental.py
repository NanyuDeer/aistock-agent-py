"""GI 盘中纯增量更新 — 单元测试（2026-08-14，Top-3 填充缺陷修复后）。

覆盖验证点：
1. 首个 bullish/bearish 正确成为 max
2. 弱事件不调用 LLM（池未满入池替补 / 池满 skip）
3. 强事件可以更新 max（规则直接替换，不调 LLM）
4. 临界事件调用 quick_think（池满场景）
5. Top-3 正确排序（降序、0→1→2→3 填充）
6. 重复 event_id 不重复比较
7. Redis 丢失可从 DB 恢复
8. quick_think 失败保留旧 max（池满场景）
9. 达到每日 LLM 上限后停止 LLM
10. GI 异常不影响事件传导（不外抛）
11. 一批多个事件先规则排序再决定 LLM
12. 不存在盘中全量 deep_think GI
13. replace=False 事件进入 Top-3 作替补（不顶掉 max）
14. Top-3 满后低分事件不进入
15. 新高分事件替换 max
16. max 始终等于 Top-3[0]
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.services.global_importance_evaluation import (
    incremental_gi,
)

pytestmark = pytest.mark.asyncio

DATE = "2026-08-14"


@pytest.fixture(autouse=True)
def _force_incremental_gi():
    """强制走增量 GI 路径，隔离本地 .env.development / .env.production 配置。

    settings 是模块加载时的单例，patch.dict(os.environ) 无法在读取后生效，
    必须直接 patch settings.gi_incremental_enabled 属性。
    """
    with patch.object(settings, "gi_incremental_enabled", True):
        yield


class _FakeRedis:
    """有状态 Redis：save 后 get 可恢复（跨批次状态保留）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value


class _FakeApi:
    """有状态 Node API：落库后 get 可恢复（DB 真源回退）。"""

    def __init__(self) -> None:
        self.report: dict | None = None

    async def get_analysis_report_quiet(self, report_type: str, report_date: str):
        return self.report

    async def save_analysis_report(self, **kwargs):
        self.report = {"content": kwargs["content"]}
        return {"id": 1}


def make_event(
    event_id: str,
    *,
    direction: str = "bullish",
    strength: float = 0.8,
    rating: str = "positive",
    industries: list[str] | None = None,
) -> dict[str, object]:
    """构造 _to_gi_events 格式的 GI 输入事件。"""
    return {
        "event_id": event_id,
        "event_time": "",
        "event_age_days": 0,
        "summary": f"summary {event_id}",
        "original_event": f"title {event_id}",
        "impact_industries": industries or ["半导体"],
        "impact_chain": [
            {"industry": "半导体", "direction": direction, "impact_strength": strength}
        ],
        "key_variables": [{"name": "var", "direction": direction, "strength": strength}],
        "mechanism": f"mechanism {event_id}",
        "investment_rating": rating,
        "investment_conclusion": f"conclusion {event_id}",
    }


@pytest.fixture(autouse=True)
def _gi_patches():
    """统一 mock：有状态 Redis + DB，跨批次保留 GI 状态。"""
    with (
        patch("aistock_agent.services.global_importance_evaluation.RedisPool") as mock_pool,
        patch("aistock_agent.services.global_importance_evaluation.node_api") as mock_api,
        patch("aistock_agent.config.settings.gi_max_llm_calls_per_day", 10),
        patch("aistock_agent.config.settings.gi_compare_epsilon", 0.1),
        patch("aistock_agent.config.settings.gi_top_k", 3),
        patch("aistock_agent.config.settings.gi_state_ttl", 86400),
    ):
        fake_redis = _FakeRedis()
        fake_api = _FakeApi()
        mock_pool.get_client = AsyncMock(return_value=fake_redis)
        mock_api.get_analysis_report_quiet = fake_api.get_analysis_report_quiet
        mock_api.save_analysis_report = fake_api.save_analysis_report
        yield mock_pool, fake_api, fake_redis


def _patched_llm(replace: bool):
    """patch _llm_compare 返回固定判定。"""
    return patch(
        "aistock_agent.services.global_importance_evaluation._llm_compare",
        new=AsyncMock(return_value={"replace": replace, "reason": "mock"}),
    )


def _state_from_save(fake_api: _FakeApi) -> dict[str, object]:
    """从 DB 报告读取最后保存的 gi_incremental_state。"""
    assert fake_api.report is not None
    content = fake_api.report["content"]
    return content["gi_incremental_state"]


def _top3_ids(state: dict[str, object], direction: str) -> list[str]:
    return [c["event_id"] for c in state.get(f"top3_{direction}") or []]


def _top3_scores(state: dict[str, object], direction: str) -> list[float]:
    return [float(c["proxy_score"]) for c in state.get(f"top3_{direction}") or []]


# ── 1. 首个 bullish/bearish 正确成为 max ──


async def test_first_event_becomes_max(_gi_patches):
    _, fake_api, _ = _gi_patches
    result = await incremental_gi(
        [
            make_event("evt1", direction="bullish", strength=0.8),
            make_event("evt2", direction="bearish", strength=0.9),
        ],
        score_date=DATE,
    )
    assert result["top_bullish_event"]["event_id"] == "evt1"
    assert result["top_bearish_event"]["event_id"] == "evt2"
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "evt1"
    assert state["max_bearish"]["event_id"] == "evt2"
    assert state["llm_used_today"] == 0


# ── 2. 弱事件不调用 LLM（池未满入池替补，不替换 max） ──


async def test_weak_event_no_llm_keeps_max(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("evt1", strength=0.9)], score_date=DATE)
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi([make_event("evt2", strength=0.2)], score_date=DATE)
        mock_llm.assert_not_awaited()
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "evt1"
    assert "evt2" in state["compared_event_ids"]


# ── 3. 强事件直接替换 max，不调 LLM ──


async def test_strong_event_replaces_max_without_llm(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("evt1", strength=0.5)], score_date=DATE)
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi([make_event("evt2", strength=0.9)], score_date=DATE)
        mock_llm.assert_not_awaited()
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "evt2"


# ── 4. 临界事件调用 quick_think（池满场景） ──


async def test_boundary_event_triggers_llm(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("e1", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.8)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.7)], score_date=DATE)  # 池满
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi([make_event("e4", strength=0.85)], score_date=DATE)
        mock_llm.assert_awaited_once()
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "e1"  # 判定不替换 → 保持


# ── 5. Top-3 排序 + 0→1→2→3 填充 ──


async def test_top3_sorted(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("evt1", strength=0.5)], score_date=DATE)
    await incremental_gi([make_event("evt2", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("evt3", strength=0.7)], score_date=DATE)
    await incremental_gi([make_event("evt4", strength=0.6)], score_date=DATE)
    state = _state_from_save(fake_api)
    scores = _top3_scores(state, "bullish")
    assert scores == sorted(scores, reverse=True)
    assert _top3_ids(state, "bullish") == ["evt2", "evt3", "evt4"]
    assert len(state["top3_bullish"]) == 3


async def test_top3_fills_0_to_3(_gi_patches):
    _, fake_api, _ = _gi_patches
    # 池未满：中间代理分事件也应进入（0→1→2→3）
    await incremental_gi([make_event("e1", strength=0.5)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.4)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.3)], score_date=DATE)
    state = _state_from_save(fake_api)
    assert _top3_ids(state, "bullish") == ["e1", "e2", "e3"]
    assert state["max_bullish"]["event_id"] == "e1"
    assert state["max_bullish"]["event_id"] == state["top3_bullish"][0]["event_id"]


# ── 6. 重复 event_id 不重复比较 ──


async def test_same_event_not_recompared(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("evt1", strength=0.8)], score_date=DATE)
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi([make_event("evt1", strength=0.8)], score_date=DATE)
        mock_llm.assert_not_awaited()
    state = _state_from_save(fake_api)
    assert state["compared_event_ids"].count("evt1") == 1


# ── 7. Redis 丢失可从 DB 恢复 ──


async def test_redis_missing_restore_from_db(_gi_patches):
    _, fake_api, fake_redis = _gi_patches
    await incremental_gi([make_event("evt1", strength=0.8)], score_date=DATE)
    assert fake_redis.store  # Redis 已写入

    fake_redis.store.clear()  # 模拟 Redis 丢失 → 应回退 DB
    with _patched_llm(replace=False):
        await incremental_gi([make_event("evt2", strength=0.85)], score_date=DATE)
    state = _state_from_save(fake_api)
    ids = [c["event_id"] for c in state["top3_bullish"]]
    assert "evt1" in ids  # DB 恢复后 evt1 仍在池中


# ── 8. quick_think 失败保留旧 max（池满场景） ──


async def test_llm_failure_keeps_old_max(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("e1", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.8)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.7)], score_date=DATE)
    with patch(
        "aistock_agent.services.global_importance_evaluation._llm_compare",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await incremental_gi([make_event("e4", strength=0.85)], score_date=DATE)
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "e1"


# ── 9. 达到每日 LLM 上限后停止 LLM ──


async def test_llm_daily_cap(_gi_patches):
    _, fake_api, _ = _gi_patches
    with patch("aistock_agent.config.settings.gi_max_llm_calls_per_day", 2):
        with _patched_llm(replace=True) as mock_llm:
            await incremental_gi([make_event("e1", strength=0.9)], score_date=DATE)
            await incremental_gi([make_event("e2", strength=0.8)], score_date=DATE)
            await incremental_gi([make_event("e3", strength=0.7)], score_date=DATE)  # 池满，无 LLM
            await incremental_gi([make_event("e4", strength=0.85)], score_date=DATE)  # 临界 → LLM#1
            await incremental_gi([make_event("e5", strength=0.82)], score_date=DATE)  # 临界 → LLM#2
            await incremental_gi([make_event("e6", strength=0.81)], score_date=DATE)  # 已达上限 → 规则
            assert mock_llm.await_count == 2


# ── 10. GI 异常不影响事件传导（不外抛） ──


async def test_gi_error_does_not_break(_gi_patches):
    _, fake_api, _ = _gi_patches
    with patch(
        "aistock_agent.services.global_importance_evaluation.node_api.save_analysis_report",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        result = await incremental_gi([make_event("evt1", strength=0.8)], score_date=DATE)
    assert result["persisted"] is False


# ── 11. 一批多个事件先规则排序再决定 LLM ──


async def test_batch_sorted_by_score(_gi_patches):
    _, fake_api, _ = _gi_patches
    # 池满后：批次 [weak(0.2), cand(0.85)] → 排序后 cand 先处理 → 触发 LLM
    await incremental_gi([make_event("e1", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.8)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.7)], score_date=DATE)
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi(
            [
                make_event("weak", strength=0.2),
                make_event("cand", strength=0.85),
            ],
            score_date=DATE,
        )
        mock_llm.assert_awaited_once()
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "e1"  # 判不替换 → 保持
    assert "cand" in _top3_ids(state, "bullish")  # replace=False 仍入池替补


# ── 12. 不存在盘中全量 deep_think GI ──


async def test_incremental_no_deep_think(_gi_patches):
    with patch(
        "aistock_agent.services.global_importance_evaluation.get_deep_think"
    ) as mock_deep:
        await incremental_gi([make_event("evt1", strength=0.8)], score_date=DATE)
        mock_deep.assert_not_called()


# ── 13. replace=False 事件进入 Top-3 作替补（不顶掉 max） ──


async def test_replace_false_enters_top3_as_backup(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("e1", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.8)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.7)], score_date=DATE)
    with _patched_llm(replace=False):
        await incremental_gi([make_event("e4", strength=0.85)], score_date=DATE)
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "e1"  # 不替换 max
    assert "e4" in _top3_ids(state, "bullish")  # 进入替补
    assert _top3_scores(state, "bullish") == sorted(_top3_scores(state, "bullish"), reverse=True)
    assert state["top3_bullish"][0]["event_id"] == "e1"  # max == Top-3[0]


# ── 14. Top-3 满后低分事件不进入 ──


async def test_top3_full_low_score_skipped(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("e1", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.8)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.7)], score_date=DATE)
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi([make_event("low", strength=0.3)], score_date=DATE)
        mock_llm.assert_not_awaited()
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "e1"
    assert "low" not in _top3_ids(state, "bullish")
    assert "low" in state["compared_event_ids"]


# ── 15. 新高分事件替换 max（规则直接，不调 LLM） ──


async def test_high_score_replaces_max(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("e1", strength=0.5)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.6)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.55)], score_date=DATE)
    with _patched_llm(replace=False) as mock_llm:
        await incremental_gi([make_event("e4", strength=0.95)], score_date=DATE)
        mock_llm.assert_not_awaited()  # 明显高于 max → 规则直接替换
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == "e4"
    assert state["top3_bullish"][0]["event_id"] == "e4"


# ── 16. max 始终等于 Top-3[0]（多批次综合） ──


async def test_max_always_equals_top3_0(_gi_patches):
    _, fake_api, _ = _gi_patches
    await incremental_gi([make_event("e1", strength=0.6)], score_date=DATE)
    await incremental_gi([make_event("e2", strength=0.9)], score_date=DATE)
    await incremental_gi([make_event("e3", strength=0.5)], score_date=DATE)
    await incremental_gi([make_event("e4", strength=0.8)], score_date=DATE)
    await incremental_gi([make_event("e5", strength=0.7)], score_date=DATE)
    with _patched_llm(replace=False):
        await incremental_gi([make_event("e6", strength=0.85)], score_date=DATE)
    state = _state_from_save(fake_api)
    assert state["max_bullish"]["event_id"] == state["top3_bullish"][0]["event_id"]
    assert _top3_scores(state, "bullish") == sorted(_top3_scores(state, "bullish"), reverse=True)
