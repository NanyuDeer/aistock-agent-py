"""归因链组装测试（spec P1a-3）。

板块溯源结果按真实落库形状构造：trace_result 为
SectorChainResult.model_dump(mode="json")（含 chain_id/sector/stages/
attribution_status/missing_evidence），而非简化的 summary 键。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.schemas.sector_trace import SectorChainResult, SectorStage
from aistock_agent.services.attribution_chain import assemble_attribution_chain


def _review_payload():
    return _review_payload_with_a_share({"index_change_pct": -1.2})


def _review_payload_with_a_share(a_share: dict[str, object]) -> dict[str, object]:
    """构造 review 报告：a_share 快照可替换（真实快照结构见下方用例）。"""
    return {
        "report": {
            "content": {
                "market_trace": {
                    "snapshot": {"a_share": a_share},
                    "trace": {"attribution_summary": "半导体材料与券商领跌拖累大盘"},
                }
            }
        }
    }


def _sector_result(
    name,
    pct,
    summary,
    *,
    status: str = "sufficient",
    with_trigger: bool = True,
):
    """构造板块溯源结果：trace_result 用真实 SectorChainResult dump 形状。"""
    stages = [
        SectorStage(kind="phenomenon", headline=f"{name}今日大幅波动"),
        SectorStage(kind="transmission", headline=f"{name}带动产业链联动"),
        SectorStage(kind="impact", headline="拖累大盘"),
    ]
    if with_trigger:
        stages.insert(1, SectorStage(kind="trigger", headline=summary, claims=[summary]))
    chain = SectorChainResult(
        chain_id=f"chain-{name}",
        sector=name,
        stages=stages,
        attribution_status=status,  # type: ignore[arg-type]
    )

    class R:
        sector = name
        trace_result = chain.model_dump(mode="json")
        snapshot = {"sector": {"name": name, "pct_change": pct}}

    return R()


def test_assemble_root_and_children():
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_result("半导体材料", -3.0, "美对华设备出口限制落地"),
            _sector_result("券商", -0.8, "大盘情绪拖累"),
        ],
    )
    assert chain["date"] == "2026-09-03"
    assert chain["root"]["type"] == "market"
    assert chain["root"]["summary"] == "半导体材料与券商领跌拖累大盘"
    assert chain["root"]["index_pct"] == -1.2
    rel = {c["sector"]: c["relation"] for c in chain["children"]}
    assert rel["半导体材料"] == "self_driven"
    assert rel["券商"] == "market_follow"
    # I-1：trace_summary 从真实溯源 dump 的 trigger stage 摘一句话
    assert chain["children"][0]["trace_summary"] == "美对华设备出口限制落地"


def test_trace_summary_extracted_from_real_chain_dump():
    """真实 SectorChainResult dump（stages 四段、sufficient）→ 取 trigger headline。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "美对华设备出口限制落地")],
    )
    assert chain["children"][0]["trace_summary"] == "美对华设备出口限制落地"


def test_trace_summary_fallback_when_insufficient():
    """I-1：attribution_status=insufficient → 不再显示'板块溯源完成'占位。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "疑似外部限制", status="insufficient")],
    )
    assert chain["children"][0]["trace_summary"] == "溯源未确认驱动原因"


def test_trace_summary_fallback_when_no_trigger_stage():
    """I-1：无 trigger stage（驱动原因未确认）→ 回退占位。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "", with_trigger=False)],
    )
    assert chain["children"][0]["trace_summary"] == "溯源未确认驱动原因"


def test_trace_summary_fallback_when_trace_result_empty():
    """I-1：空/非 dict trace_result（无法提取）→ 回退占位。"""

    class R:
        sector = "板块A"
        trace_result = {}
        snapshot = {"sector": {"name": "板块A", "pct_change": 1.0}}

    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload={
            "report": {
                "content": {
                    "market_trace": {"snapshot": {"a_share": {}}, "trace": {}}
                }
            }
        },
        sector_results=[R()],
    )
    assert chain["children"][0]["trace_summary"] == "溯源未确认驱动原因"


@pytest.mark.asyncio
async def test_save_posts_to_api_internal():
    """写入路径必须带 /api 前缀（Critical 2026-09-17 防回归）。

    app-api 中 attributionChainRouter 挂在 ``/api`` 下（index.ts:165），其写接口绝对路径为
    ``/api/internal/attribution-chain``；而 ``/internal`` 是另一个 router 的挂载点
    （index.ts:631）。缺 /api 前缀 → 路由不匹配 → 恒 404 → post 吞错返回 None →
    链静默不落库（生产 attribution_chains 表曾长期不存在）。
    """
    from aistock_agent.services.attribution_chain import AttributionChainStore

    store = AttributionChainStore()
    with patch.object(
        store.node_api, "post", new=AsyncMock(return_value={"ok": True})
    ) as mock_post:
        await store.save(
            "2026-09-03",
            {"date": "2026-09-03", "root": {"type": "market"}, "children": []},
        )
        mock_post.assert_awaited_once()
        call = mock_post.await_args
        assert call.args[0].startswith("/api/internal/attribution-chain")
        # 负向断言：path 以 /internal/ 开头即表示漏了 /api 前缀（该前缀下无此路由，恒 404）
        assert not call.args[0].startswith("/internal/")
        assert call.args[1]["date"] == "2026-09-03"
        assert call.args[1]["chain"]["root"]["type"] == "market"


@pytest.mark.asyncio
async def test_save_warns_when_post_returns_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M-1：node_api.post 吞错返回 None（data_client.post 失败语义）→ warning 而非 saved。"""
    from aistock_agent.services.attribution_chain import AttributionChainStore

    store = AttributionChainStore()
    with patch.object(store.node_api, "post", new=AsyncMock(return_value=None)) as mock_post:
        await store.save(
            "2026-09-03",
            {"date": "2026-09-03", "root": {"type": "market"}, "children": []},
        )
        mock_post.assert_awaited_once()
    out = capsys.readouterr().out
    assert "attribution_chain.save_failed" in out
    assert "attribution_chain.saved" not in out


def test_no_index_relation_unknown():
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload={
            "report": {
                "content": {
                    "market_trace": {"snapshot": {"a_share": {}}, "trace": {}}
                }
            }
        },
        sector_results=[_sector_result("板块A", 1.0, "事件驱动")],
    )
    assert chain["root"]["index_pct"] is None
    assert chain["children"][0]["relation"] == "unknown"
    assert chain["root"]["summary"] == ""


# --- 大盘涨跌幅真实键（a_share.indexes）解析：修复 relation 恒 unknown ---


def _chain_with_a_share(a_share: dict[str, object], *, sector_pct: float = -3.0):
    return assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload_with_a_share(a_share),
        sector_results=[_sector_result("半导体材料", sector_pct, "美对华设备出口限制落地")],
    )


def test_index_pct_from_real_snapshot_indexes_list():
    """真实快照结构（a_share.indexes 列表）→ root.index_pct = 上证涨跌幅。

    旧实现读 index_change_pct 等四个不存在的键 → 恒 None → relation 恒 unknown。
    """
    chain = _chain_with_a_share(
        {"indexes": [{"name": "上证指数", "code": "000001", "change_pct": -0.9}]}
    )
    assert chain["root"]["index_pct"] == -0.9
    # 板块跌幅显著大于指数 → self_driven（不再是 unknown）
    assert chain["children"][0]["relation"] == "self_driven"
    assert (
        judge_sector_driver_relation(
            chain["children"][0]["pct"], chain["root"]["index_pct"]
        )
        == "self_driven"
    )


def test_index_pct_prefers_shanghai_over_first_item_in_normalized_dict():
    """归一化产出形状（indexes 为 dict，key=SH000001）→ 取上证而非首个深证。"""
    chain = _chain_with_a_share(
        {
            "indexes": {
                "SZ399001": {"ts_code": "399001.SZ", "name": "深证成指", "change_pct": -1.8},
                "SH000001": {"ts_code": "000001.SH", "name": "上证指数", "change_pct": -0.9},
            }
        }
    )
    assert chain["root"]["index_pct"] == -0.9


def test_index_pct_falls_back_to_first_index_without_shanghai():
    """无上证指数 → 取列表第一项（另有深证成指时取深证）。"""
    chain = _chain_with_a_share(
        {
            "indexes": [
                {"name": "深证成指", "code": "399001.SZ", "change_pct": -1.8},
                {"name": "创业板指", "code": "399006.SZ", "change_pct": -2.4},
            ]
        }
    )
    assert chain["root"]["index_pct"] == -1.8


def test_index_pct_legacy_keys_still_supported():
    """向后兼容：indexes 缺失时仍按旧四键读取。"""
    assert _chain_with_a_share({"index_change_pct": -1.2})["root"]["index_pct"] == -1.2
    assert _chain_with_a_share({"sh_change_pct": -0.7})["root"]["index_pct"] == -0.7


def test_index_pct_non_numeric_change_pct_falls_back_then_none():
    """indexes 值非数值 → 视为缺失并回退旧键；全部缺失 → None（不伪造 0）。"""
    assert (
        _chain_with_a_share(
            {"indexes": [{"name": "上证指数", "change_pct": "x"}], "index_pct": 0.8}
        )["root"]["index_pct"]
        == 0.8
    )
    assert (
        _chain_with_a_share({"indexes": [{"name": "上证指数", "change_pct": None}]})[
            "root"
        ]["index_pct"]
        is None
    )


# --- Task 2.1：链事件节点契约（children[].events: warehouse/search，spec §3.2-4） ---

# 与 sector_trace_snapshot._sector_evidence_queries 第 1 组同形（检索补漏来源的 kind 后缀）
_SEARCH_QUERY = "2026-09-03 半导体材料 板块 暴跌 大涨 原因"


def _warehouse_event(
    event_id: str | None,
    title: str,
    *,
    url: str = "",
    impact_score: int = 5,
    keywords: list[str] | None = None,
    industry: str = "",
    summary: str = "",
) -> dict[str, object]:
    """中台存量事件（event_store.EventRecord 消费侧最小形状）。"""
    return {
        "event_id": event_id,
        "title": title,
        "summary": summary,
        "url": url,
        "impact_score": impact_score,
        "involved_keywords": keywords or [],
        "industry": industry,
    }


def _search_source(
    title: str,
    *,
    url: str = "",
    content: str = "板块当日大幅波动，市场关注政策动向。",
    query: str = _SEARCH_QUERY,
) -> dict[str, object]:
    """板块定向检索来源（sector_trace_snapshot._normalize_source 产物形状）。"""
    return {
        "title": title,
        "url": url,
        "content": content,
        "published_at": "2026-09-03T10:00:00Z",
        "kind": f"sector_event:{query}",
        "source": "tavily_finance_search",
    }


def _sector_with_evidence(
    name: str,
    pct: float,
    summary: str = "美对华设备出口限制落地",
    *,
    sources: list[dict[str, object]] | None = None,
    trigger_evidence: list[dict[str, object]] | None = None,
    attribution_parent: dict[str, object] | None = None,
):
    """板块溯源结果（真实 dump 形状 + 快照 sources + 可选报告 attribution_parent）。"""
    stages = [
        SectorStage(kind="phenomenon", headline=f"{name}今日大幅波动"),
        SectorStage(
            kind="trigger",
            headline=summary,
            claims=[summary],
            evidence=trigger_evidence or [],
        ),
        SectorStage(kind="transmission", headline=f"{name}带动产业链联动"),
        SectorStage(kind="impact", headline="拖累大盘"),
    ]
    chain = SectorChainResult(
        chain_id=f"chain-{name}",
        sector=name,
        stages=stages,
        attribution_status="sufficient",
    )

    class R:
        sector = name
        trace_result = chain.model_dump(mode="json")
        snapshot = {"sector": {"name": name, "pct_change": pct}, "sources": sources or []}

    if attribution_parent is not None:
        R.attribution_parent = attribution_parent  # type: ignore[attr-defined]
    return R()


def test_child_events_warehouse_hit_keeps_event_id():
    """中台命中 → source='warehouse' 且 event_id 非空（ref 为可追溯 URL）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_with_evidence("半导体材料", -3.0)],
        warehouse_events=[
            _warehouse_event(
                "2026-09-03-abc1234567890",
                "美对华半导体设备出口限制落地",
                url="https://news.example.com/a",
                impact_score=9,
                keywords=["半导体", "出口限制"],
            )
        ],
    )
    assert chain["children"][0]["events"] == [
        {
            "event_id": "2026-09-03-abc1234567890",
            "ref": "https://news.example.com/a",
            "headline": "美对华半导体设备出口限制落地",
            "source": "warehouse",
        }
    ]


def test_child_events_search_fallback_when_warehouse_miss():
    """中台无命中但检索有 → source='search'，event_id 为 null（不冒充中台 id）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[_search_source("半导体材料板块大跌 出口管制升级", url="https://news.example.com/b")],
            )
        ],
    )
    assert chain["children"][0]["events"] == [
        {
            "event_id": None,
            "ref": "https://news.example.com/b",
            "headline": "半导体材料板块大跌 出口管制升级",
            "source": "search",
        }
    ]


def test_child_events_empty_when_no_hit():
    """中台与检索都无命中 → events 为空数组，不编造 event_id/URL。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[_search_source("白酒龙头半年报点评", url="https://news.example.com/c")],
            )
        ],
        warehouse_events=[_warehouse_event("2026-09-03-zzz", "白酒库存周期见底")],
    )
    assert chain["children"][0]["events"] == []


def test_child_events_dedup_same_event_keeps_warehouse_only():
    """同一现实事件（同 URL）不产生两个节点：中台优先，检索补漏被吸收。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[_search_source("半导体材料设备出口限制落地", url="https://news.example.com/a")],
            )
        ],
        warehouse_events=[
            _warehouse_event(
                "2026-09-03-abc1234567890",
                "美对华半导体设备出口限制落地",
                url="https://news.example.com/a",
                keywords=["半导体材料"],
            )
        ],
    )
    events = chain["children"][0]["events"]
    assert len(events) == 1
    assert events[0]["source"] == "warehouse"


def test_child_events_dedup_similar_title_across_search_sources():
    """检索来源标题仅标点/空格差异（归一化相同）→ 只留一条。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[
                    _search_source("半导体材料板块大跌：出口管制升级", url="https://news.example.com/d1"),
                    _search_source("半导体材料板块大跌 出口管制升级", url="https://news.example.com/d2"),
                ],
            )
        ],
    )
    events = chain["children"][0]["events"]
    assert len(events) == 1
    assert events[0]["headline"] == "半导体材料板块大跌：出口管制升级"


def test_child_events_cap_and_ranking_by_impact():
    """每板块上限 3 条，取最相关（权重同分按 impact_score 降序）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_with_evidence("半导体材料", -3.0)],
        warehouse_events=[
            _warehouse_event(f"2026-09-03-e{i}", f"半导体材料相关事件{i}", impact_score=score)
            for i, score in enumerate([4, 9, 6, 8, 5])
        ],
    )
    headlines = [e["headline"] for e in chain["children"][0]["events"]]
    assert headlines == ["半导体材料相关事件1", "半导体材料相关事件3", "半导体材料相关事件2"]


def test_child_events_warehouse_without_event_id_skipped():
    """契约要求 warehouse 节点 event_id 非空 → 无 id 的中台事件不产节点（宁缺不造）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_with_evidence("半导体材料", -3.0)],
        warehouse_events=[
            _warehouse_event(None, "半导体材料出口限制落地", url="https://news.example.com/e"),
        ],
    )
    assert chain["children"][0]["events"] == []


def test_child_events_search_ref_falls_back_to_query_and_title():
    """检索来源无 URL → ref 用「检索 query + title」保留可追溯引用。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料", -3.0, sources=[_search_source("半导体材料出口管制升级")]
            )
        ],
    )
    assert chain["children"][0]["events"][0]["ref"] == (
        f"search:{_SEARCH_QUERY}|半导体材料出口管制升级"
    )


def test_child_events_prefers_trigger_evidence_source():
    """检索补漏排序：溯源 trigger 阶段引用的来源（最贴近根因）优先于检索原始序。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[
                    _search_source("半导体材料板块今日收评：资金净流出", url="https://news.example.com/f1"),
                    _search_source("半导体材料出口管制升级落地", url="https://news.example.com/f2"),
                ],
                trigger_evidence=[
                    {"url": "https://news.example.com/f2", "title": "半导体材料出口管制升级落地"}
                ],
            )
        ],
    )
    assert [e["ref"] for e in chain["children"][0]["events"]] == [
        "https://news.example.com/f2",
        "https://news.example.com/f1",
    ]


# --- Task 2.2：消费 sector_trace 报告的 attribution_parent（修"只写不读"） ---


def test_root_index_pct_consistent_with_parent_ref():
    """报告 attribution_parent.index_pct 与快照一致 → 组装结果与报告一致。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                attribution_parent={
                    "source_report_type": "review",
                    "report_date": "2026-09-03",
                    "index_pct": -1.2,
                },
            )
        ],
    )
    assert chain["root"]["index_pct"] == -1.2
    assert chain["children"][0]["relation"] == "self_driven"


def test_root_index_pct_report_wins_on_mismatch_with_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """报告 index_pct 与快照不一致 → 以报告为准 + warning chain_parent_mismatch（不静默）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),  # 快照 -1.2
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -2.0,
                attribution_parent={
                    "source_report_type": "review",
                    "report_date": "2026-09-03",
                    "index_pct": -0.4,
                },
            )
        ],
    )
    out = capsys.readouterr().out
    assert "chain_parent_mismatch" in out
    assert chain["root"]["index_pct"] == -0.4  # 以报告为准
    # relation 随报告口径重算：|−2.0| > 2*|−0.4| + 0.5 → self_driven
    assert chain["children"][0]["relation"] == "self_driven"


def test_root_index_pct_filled_from_parent_ref_when_snapshot_missing() -> None:
    """快照无大盘涨跌幅但报告有 → 用报告补全（不是 mismatch，不打 warning）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload={
            "report": {
                "content": {
                    "market_trace": {"snapshot": {"a_share": {}}, "trace": {}}
                }
            }
        },
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                2.0,
                attribution_parent={"index_pct": 0.4},
            )
        ],
    )
    assert chain["root"]["index_pct"] == 0.4
    assert chain["children"][0]["relation"] == "self_driven"


def test_root_index_pct_unchanged_without_parent_ref() -> None:
    """报告缺 attribution_parent → 行为与现状一致（快照口径，不告警）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_with_evidence("半导体材料", -0.8)],
    )
    assert chain["root"]["index_pct"] == -1.2
    assert chain["children"][0]["relation"] == "market_follow"


# --- Task 9.1：弱依据标注（children[].extraction + root.evidence_weak） ---

# 弱依据兜底文案（原文空缺时的中性表述：不编造主因）
_WEAK_SUMMARY = "证据不足，未确认主因"


def _review_payload_with_trace(summary: str = "", status: str = "hypothesis") -> dict:
    """review 报告：attribution_summary/attribution_status 可替换（弱归因日形态）。"""
    return {
        "report": {
            "content": {
                "market_trace": {
                    "snapshot": {"a_share": {"index_change_pct": -1.2}},
                    "trace": {
                        "attribution_summary": summary,
                        "attribution_status": status,
                    },
                }
            }
        }
    }


def _sector_with_extraction(
    name: str,
    pct: float,
    source: str,
    *,
    summary: str = "金属铅领跌带动有色走弱",
    weak: bool = True,
):
    """板块溯源结果 + 提取来源/弱标记（SectorTraceRunResult.extraction 的真实形状）。"""
    result = _sector_with_evidence(name, pct, summary)
    result.extraction = {"source": source, "weak": weak}
    return result


def test_weak_fallback_marks_child_and_root() -> None:
    """T3 快照兜底：children[].extraction 标弱 + root.evidence_weak/attribution_status。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace(),
        sector_results=[
            _sector_with_extraction("金属铅", -5.1, "snapshot"),
            _sector_with_extraction("金属锌", -4.8, "snapshot"),
        ],
    )
    assert chain["children"][0]["extraction"] == {"source": "snapshot", "weak": True}
    assert chain["children"][1]["extraction"] == {"source": "snapshot", "weak": True}
    assert chain["root"]["evidence_weak"] is True
    assert chain["root"]["attribution_status"] == "hypothesis"
    # 原文空缺 → 中性表述，不编造主因
    assert chain["root"]["summary"] == _WEAK_SUMMARY


def test_weak_fallback_keeps_report_summary_when_present() -> None:
    """原文有结论 → 保留原文（兜底只补空缺，不覆盖已确认文案）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("板块普跌，情绪主导"),
        sector_results=[_sector_with_extraction("金属铅", -5.1, "candidate_claim")],
    )
    assert chain["root"]["summary"] == "板块普跌，情绪主导"
    assert chain["children"][0]["extraction"] == {
        "source": "candidate_claim",
        "weak": True,
    }


def test_weak_fallback_omits_status_when_report_lacks_it() -> None:
    """报告无 attribution_status → 不编造（仅 evidence_weak 标弱）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace(status=""),
        sector_results=[_sector_with_extraction("金属铅", -5.1, "snapshot")],
    )
    assert chain["root"]["evidence_weak"] is True
    assert "attribution_status" not in chain["root"]


def test_primary_hit_carries_no_weak_marks() -> None:
    """T1 主链命中 → 不写 extraction / evidence_weak（正常链不被弱标记污染）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("半导体材料领跌"),
        sector_results=[
            _sector_with_extraction(
                "半导体材料", -3.0, "primary_claim", weak=False
            )
        ],
    )
    assert "extraction" not in chain["children"][0]
    assert "evidence_weak" not in chain["root"]
    assert "attribution_status" not in chain["root"]
    assert chain["root"]["summary"] == "半导体材料领跌"


def test_missing_extraction_attr_is_treated_as_primary() -> None:
    """无 extraction 属性（旧调用方/回放）→ 与 T1 同形，不误标弱。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("半导体材料领跌"),
        sector_results=[_sector_with_evidence("半导体材料", -3.0)],
    )
    assert "extraction" not in chain["children"][0]
    assert "evidence_weak" not in chain["root"]
