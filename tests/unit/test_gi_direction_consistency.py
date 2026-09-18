"""GI 方向一致性 + 候选替换 — 单元测试（2026-09-02）。

覆盖验证点（对应需求回归验证）：
1. chain_dominant_direction：bullish 主导 / bearish 主导 / 接近→neutral / 1.5x 边界 / 空
2. 全量 GI：GI bullish + chain bearish → reject 并自动替换下一候选
3. 全量 GI：GI bearish + chain bullish → reject 并自动替换下一候选
4. 全量 GI：bullish/bearish 接近 → neutral → reject
5. 全量 GI：方向一致 → 保留原候选
6. 全量 GI：无合格候选 → 置空（记录日志，允许为空）
7. 增量 GI：max 与 chain 主导方向冲突 → 沿 Top-K 取合格候选
8. 增量 GI：全部冲突 → 置空
9. 增量 GI：旧数据无 chain_direction → 放行（fail-open）
"""

from aistock_agent.services.global_importance_evaluation import (
    _consistent_candidate,
    _enforce_full_gi_consistency,
    _state_to_result,
    chain_dominant_direction,
)


def make_chain(directions: list[tuple[str, float]]) -> list[dict[str, object]]:
    """构造 impact_chain：[(direction, impact_strength), ...]。"""
    return [
        {"industry": f"ind{i}", "direction": d, "impact_strength": s}
        for i, (d, s) in enumerate(directions)
    ]


def make_event(event_id: str, chain: list[dict[str, object]]) -> dict[str, object]:
    return {
        "event_id": event_id,
        "summary": f"summary {event_id}",
        "original_event": f"title {event_id}",
        "impact_chain": chain,
        "impact_industries": [x["industry"] for x in chain],
        "key_variables": [],
        "mechanism": "mech",
        "investment_rating": "positive",
        "investment_conclusion": f"conclusion {event_id}",
    }


# ── chain_dominant_direction ──


def test_chain_dominant_bullish():
    assert chain_dominant_direction(make_chain([("bullish", 0.8), ("bearish", 0.3)])) == "bullish"


def test_chain_dominant_bearish():
    assert chain_dominant_direction(make_chain([("bullish", 0.3), ("bearish", 0.8)])) == "bearish"


def test_chain_dominant_neutral_when_close():
    assert chain_dominant_direction(make_chain([("bullish", 0.5), ("bearish", 0.4)])) == "neutral"


def test_chain_dominant_boundary():
    # bullish 0.75 == bearish 0.5 × 1.5 → 边界命中 bullish（>=，二进制可精确表示避免浮点误差）
    assert chain_dominant_direction(make_chain([("bullish", 0.75), ("bearish", 0.5)])) == "bullish"


def test_chain_dominant_empty_or_invalid():
    assert chain_dominant_direction([]) == "neutral"
    assert chain_dominant_direction(None) == "neutral"


# ── 全量 GI 候选替换 ──


def test_full_gi_bullish_chain_bearish_rejected_and_replaced():
    """GI bullish + chain bearish → 拒绝 e1，自动替换为 chain bullish 的 e2。"""
    events = [
        make_event("e1", make_chain([("bearish", 0.8), ("bullish", 0.2)])),
        make_event("e2", make_chain([("bullish", 0.7), ("bearish", 0.2)])),
    ]
    top_bullish = {
        "event_id": "e1", "direction": "bullish",
        "importance_level": "critical", "reason": "x",
    }
    new_bull, new_bear = _enforce_full_gi_consistency(top_bullish, None, events)
    assert new_bull is not None
    assert new_bull["event_id"] == "e2"
    assert new_bull["direction"] == "bullish"
    assert new_bear is None


def test_full_gi_bearish_chain_bullish_rejected_and_replaced():
    """GI bearish + chain bullish → 拒绝 e1，自动替换为 chain bearish 的 e2。"""
    events = [
        make_event("e1", make_chain([("bullish", 0.8), ("bearish", 0.2)])),
        make_event("e2", make_chain([("bearish", 0.7), ("bullish", 0.2)])),
    ]
    top_bearish = {
        "event_id": "e1", "direction": "bearish",
        "importance_level": "critical", "reason": "x",
    }
    new_bull, new_bear = _enforce_full_gi_consistency(None, top_bearish, events)
    assert new_bear is not None
    assert new_bear["event_id"] == "e2"
    assert new_bear["direction"] == "bearish"
    assert new_bull is None


def test_full_gi_neutral_chain_rejected():
    """bullish/bearish 接近 → neutral → 拒绝（事件池无合格候选 → 置空）。"""
    events = [make_event("e1", make_chain([("bullish", 0.5), ("bearish", 0.4)]))]
    top_bullish = {
        "event_id": "e1", "direction": "bullish",
        "importance_level": "critical", "reason": "x",
    }
    new_bull, _ = _enforce_full_gi_consistency(top_bullish, None, events)
    assert new_bull is None


def test_full_gi_consistent_kept():
    """GI bullish + chain bullish → 保留原候选。"""
    events = [make_event("e1", make_chain([("bullish", 0.8), ("bearish", 0.1)]))]
    top_bullish = {
        "event_id": "e1", "direction": "bullish",
        "importance_level": "critical", "reason": "x",
    }
    new_bull, _ = _enforce_full_gi_consistency(top_bullish, None, events)
    assert new_bull is not None
    assert new_bull["event_id"] == "e1"


def test_full_gi_no_valid_candidate_returns_none():
    """所有候选都不合格 → 允许为空。"""
    events = [make_event("e1", make_chain([("bearish", 0.8)]))]
    top_bullish = {
        "event_id": "e1", "direction": "bullish",
        "importance_level": "critical", "reason": "x",
    }
    new_bull, _ = _enforce_full_gi_consistency(top_bullish, None, events)
    assert new_bull is None


# ── 增量 GI：_state_to_result / _consistent_candidate ──


def test_state_to_result_max_conflict_replaced_from_top3():
    """max_bullish 的 chain_direction=bearish（冲突）→ 沿 Top-K 取 chain bullish 的 e2。"""
    state = {
        "date": "2026-09-02",
        "max_bullish": {
            "event_id": "e1", "title": "t1", "direction": "bullish", "proxy_score": 0.9,
            "importance_level": "critical", "reason": "r", "chain_direction": "bearish",
        },
        "top3_bullish": [
            {
                "event_id": "e1", "title": "t1", "direction": "bullish", "proxy_score": 0.9,
                "importance_level": "critical", "reason": "r", "chain_direction": "bearish",
            },
            {
                "event_id": "e2", "title": "t2", "direction": "bullish", "proxy_score": 0.8,
                "importance_level": "important", "reason": "r2", "chain_direction": "bullish",
            },
        ],
        "max_bearish": None,
        "top3_bearish": [],
    }
    result = _state_to_result(state)
    assert result["top_bullish_event"]["event_id"] == "e2"
    assert result["top_bullish_event"]["direction"] == "bullish"
    assert result["top_bearish_event"] is None


def test_state_to_result_all_conflict_none():
    """max 与全部 Top-K 都方向冲突 → 置空。"""
    state = {
        "date": "2026-09-02",
        "max_bullish": {
            "event_id": "e1", "title": "t1", "direction": "bullish", "proxy_score": 0.9,
            "importance_level": "critical", "reason": "r", "chain_direction": "bearish",
        },
        "top3_bullish": [
            {
                "event_id": "e1", "title": "t1", "direction": "bullish", "proxy_score": 0.9,
                "importance_level": "critical", "reason": "r", "chain_direction": "bearish",
            },
        ],
        "max_bearish": None,
        "top3_bearish": [],
    }
    result = _state_to_result(state)
    assert result["top_bullish_event"] is None


def test_state_to_result_unknown_chain_direction_allowed():
    """旧数据无 chain_direction → 放行（fail-open，无法校验不误杀）。"""
    state = {
        "date": "2026-09-02",
        "max_bullish": {
            "event_id": "e1", "title": "t1", "direction": "bullish", "proxy_score": 0.9,
            "importance_level": "critical", "reason": "r",
        },
        "top3_bullish": [],
        "max_bearish": None,
        "top3_bearish": [],
    }
    result = _state_to_result(state)
    assert result["top_bullish_event"]["event_id"] == "e1"


def test_consistent_candidate_prefers_matching_max():
    top3 = [
        {"event_id": "a", "chain_direction": "bullish", "proxy_score": 0.9},
        {"event_id": "b", "chain_direction": "bearish", "proxy_score": 0.8},
    ]
    cand = _consistent_candidate(top3[0], top3, "bullish")
    assert cand["event_id"] == "a"


def test_consistent_candidate_skips_conflict():
    top3 = [
        {"event_id": "a", "chain_direction": "bearish", "proxy_score": 0.9},
        {"event_id": "b", "chain_direction": "bullish", "proxy_score": 0.8},
    ]
    cand = _consistent_candidate(top3[0], top3, "bullish")
    assert cand["event_id"] == "b"
