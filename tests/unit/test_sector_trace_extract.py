"""多主驱动板块提取测试（spec P1a-1）+ 弱归因日三级兜底（Task 9.1）。

三级兜底口径（2026-09-17 生产缺口：4 个候选全 weak 且 primary_chain_id 为空 →
主链无命中 → 板块提取返回 [] → 溯源整链空转）：
- T1 主链 claim 命中（现有行为，逐字不变）→ source=primary_claim，非弱依据；
- T2 候选链 claim 命中（含 status=weak）→ source=candidate_claim，弱依据；
- T3 快照头部兜底（top_losers → top_gainers）→ source=snapshot，弱依据。
仅在上一级无产出时降级；跨层/跨来源按板块名去重，上限 max_sectors。
"""

from aistock_agent.agents.workers.sector_trace import (
    SOURCE_CANDIDATE_CLAIM,
    SOURCE_PRIMARY_CLAIM,
    SOURCE_SNAPSHOT,
    extract_primary_sectors,
)


def _report(
    claims: list[str] | None = None,
    losers: list[str] | None = None,
    gainers: list[str] | None = None,
    *,
    candidates: list[dict] | None = None,
    primary_chain_id: str | None = "c1",
    a_share: dict | None = None,
) -> dict:
    """构造 review 报告；candidates/a_share 可替换（供三级兜底与畸形快照用例）。"""
    if candidates is None:
        candidates = [{"id": "c1", "claims": list(claims or []), "status": "weak"}]
    if a_share is None:
        a_share = {
            "sectors": {
                "top_losers": [{"name": n} for n in (losers or [])],
                "top_gainers": [{"name": n} for n in (gainers or [])],
            }
        }
    return {
        "report": {
            "content": {
                "market_trace": {
                    "snapshot": {"a_share": a_share},
                    "trace": {
                        "primary_chain_id": primary_chain_id,
                        "candidates": [
                            {
                                "id": c["id"],
                                "status": c.get("status", "weak"),
                                "chain": {"nodes": [{"claim": claim} for claim in c["claims"]]},
                            }
                            for c in candidates
                        ],
                    },
                }
            }
        }
    }


# --- T1 主链命中（既有行为回归，逐字不变） ---


def test_losers_priority_and_multiple_hits():
    payload = _report(
        claims=["半导体材料领跌拖累大盘", "券商板块同步走弱"],
        losers=["半导体材料", "券商"],
        gainers=["半导体材料"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["半导体材料", "券商"]
    # T1 命中即来源 primary_claim，不属于弱依据（正常路径不得被弱标记污染）
    assert [h.source for h in got] == [SOURCE_PRIMARY_CLAIM, SOURCE_PRIMARY_CLAIM]
    assert [h.weak for h in got] == [False, False]


def test_gainers_fallback_when_no_loser_hit():
    payload = _report(claims=["英伟达财报催化 AI算力链领涨"], losers=[], gainers=["AI算力"])
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["AI算力"]


def test_dedup_and_max_sectors():
    payload = _report(
        claims=["板块A领涨", "板块A带动板块B", "板块B继续走强", "板块C跟涨", "板块D联动"],
        gainers=["板块A", "板块B", "板块C", "板块D"],
    )
    got = extract_primary_sectors(payload, max_sectors=3)
    assert [h.name for h in got] == ["板块A", "板块B", "板块C"]


def test_primary_hit_does_not_fall_through_to_snapshot():
    """T1 有产出即不降级：上限内不用 T2/T3 补齐。"""
    got = extract_primary_sectors(_report(claims=["金属铅领跌"], losers=["金属铅", "金属锌"]))
    assert [(h.name, h.source) for h in got] == [("金属铅", SOURCE_PRIMARY_CLAIM)]


def test_all_tiers_miss_returns_empty():
    assert extract_primary_sectors(_report(claims=["外盘大跌传导"], losers=[])) == []


# --- T2 候选链兜底（primary_chain_id 为空 / 主链无命中） ---


def test_candidate_chain_fallback_when_primary_chain_id_empty():
    """生产形态：primary_chain_id 为空 + 候选全 weak → 按候选顺序收 claim 命中板块。"""
    payload = _report(
        candidates=[
            {"id": "c1", "claims": ["弱候选：金属铅领跌"], "status": "weak"},
            {"id": "c2", "claims": ["金属锌跟随下跌"], "status": "weak"},
        ],
        primary_chain_id=None,
        losers=["金属铅", "金属锌", "黄金概念"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["金属铅", "金属锌"]
    assert [h.source for h in got] == [SOURCE_CANDIDATE_CLAIM, SOURCE_CANDIDATE_CLAIM]
    assert all(h.weak for h in got)


def test_candidate_chain_maintains_candidate_order():
    """T2 保持 candidate 顺序（候选 2 命中项位于候选 1 命中项之后）。"""
    payload = _report(
        candidates=[
            {"id": "c1", "claims": ["全球风险与流动性收紧"], "status": "weak"},
            {"id": "c2", "claims": ["金属锌领跌"], "status": "weak"},
            {"id": "c3", "claims": ["金属铅同步走弱"], "status": "weak"},
        ],
        primary_chain_id=None,
        losers=["金属铅", "金属锌"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["金属锌", "金属铅"]


def test_candidate_chain_dedup_across_candidates():
    """同一板块只收一次（保留最先命中来源），后续候选的同名 claim 不再收。"""
    payload = _report(
        candidates=[
            {"id": "c1", "claims": ["金属铅领跌"], "status": "weak"},
            {"id": "c2", "claims": ["金属铅带动金属锌"], "status": "weak"},
        ],
        primary_chain_id=None,
        losers=["金属铅", "金属锌"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["金属铅", "金属锌"]


def test_candidate_chain_respects_max_sectors():
    payload = _report(
        candidates=[{"id": "c1", "claims": ["板块A领涨板块B"], "status": "weak"}],
        primary_chain_id=None,
        gainers=["板块A", "板块B", "板块C"],
    )
    got = extract_primary_sectors(payload, max_sectors=2)
    assert [h.name for h in got] == ["板块A", "板块B"]


def test_no_hit_and_empty_snapshot_returns_empty():
    """三层都无产出（快照为空）→ 返回空且不报错。"""
    assert extract_primary_sectors(_report(claims=["外盘大跌传导"])) == []


# --- T3 快照兜底（跌市优先，不足补涨市） ---


def test_snapshot_fallback_uses_losers_head_when_claims_miss():
    payload = _report(
        claims=["外盘大跌传导"],
        losers=["金属铅", "金属锌", "黄金概念"],
        gainers=["PET铜箔"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["金属铅", "金属锌", "黄金概念"]
    assert [h.source for h in got] == [SOURCE_SNAPSHOT] * 3
    assert all(h.weak for h in got)
    # 快照行原样透传（下游 build_sector_snapshot 需要 pct_change 等字段）
    assert got[0].row == {"name": "金属铅"}


def test_snapshot_fallback_fills_from_gainers_head():
    """top_losers 头部不足上限 → 用 top_gainers 头部补齐（跌市优先语义不变）。"""
    payload = _report(
        claims=["外盘大跌传导"],
        losers=["金属铅"],
        gainers=["PET铜箔", "MLCC", "转基因"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["金属铅", "PET铜箔", "MLCC"]


def test_snapshot_fallback_dedup_and_max_sectors():
    payload = _report(
        claims=["外盘大跌传导"],
        losers=["金属铅", "金属铅"],
        gainers=["金属铅", "PET铜箔", "MLCC"],
    )
    got = extract_primary_sectors(payload, max_sectors=2)
    assert [h.name for h in got] == ["金属铅", "PET铜箔"]


def test_snapshot_fallback_skips_blank_names():
    payload = _report(
        claims=["外盘大跌传导"],
        losers=["", "金属铅"],
        gainers=["MLCC"],
    )
    got = extract_primary_sectors(payload)
    assert [h.name for h in got] == ["金属铅", "MLCC"]


# --- 畸形输入不崩 ---


def test_malformed_payloads_return_empty_without_raising():
    malformed: list[dict] = [
        {},
        {"report": None},
        {"report": {}},
        {"report": {"content": None}},
        {"report": {"content": {}}},
        _report(claims=["外盘大跌传导"], a_share={}),
        _report(claims=["外盘大跌传导"], a_share={"sectors": None}),
        _report(claims=["外盘大跌传导"], a_share={"sectors": {"top_losers": "oops"}}),
        _report(claims=["外盘大跌传导"], a_share={"sectors": {"top_losers": [None, 1]}}),
    ]
    for payload in malformed:
        assert extract_primary_sectors(payload) == []
