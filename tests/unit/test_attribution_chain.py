"""归因链组装测试（spec P1a-3）。

板块溯源结果按真实落库形状构造：trace_result 为
SectorChainResult.model_dump(mode="json")（含 chain_id/sector/stages/
attribution_status/missing_evidence），而非简化的 summary 键。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.schemas.sector_trace import SectorChainResult, SectorStage
from aistock_agent.services.attribution_chain import (
    assemble_attribution_chain,
    is_driving_event,
)


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


def test_trace_summary_taken_from_report_when_insufficient():
    """B（2026-09-18）：报告有归因句时链摘要取它——`insufficient` **不得**触发兜底覆盖。

    生产实证（2026-09-17 玉米）：同一板块在 `GET /api/agent/sector-insight/:date` 的
    `trace.summary` 有内容（"未出现单一独立公告；催化来自…"），链却是
    "溯源未确认驱动原因"；根因是旧实现拿 `attribution_status == "insufficient"`
    直接返回兜底文案（app-api `extractTraceSummary` 从不看该字段）。
    两个前端页面按摘要判"有无归因"，口径必须一致 → 有内容就不许覆盖。
    """
    summary = "未出现单一独立公告；催化来自超强厄尔尼诺供给扰动预期"
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("玉米", -3.0, summary, status="insufficient")],
    )
    assert chain["children"][0]["trace_summary"] == summary


def test_trace_summary_prefers_report_summary_field():
    """B：报告自身带非空 summary（字段形态）→ 优先取它（trigger 只是次选）。"""

    class R:
        sector = "半导体材料"
        trace_result = {
            "summary": "报告主句：设备出口限制落地",
            "stages": [
                {"kind": "phenomenon", "headline": "今日大幅波动", "claims": []},
                {"kind": "trigger", "headline": "触发句", "claims": []},
            ],
            "attribution_status": "sufficient",
        }
        snapshot = {"sector": {"name": "半导体材料", "pct_change": -3.0}}

    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[R()],
    )
    assert chain["children"][0]["trace_summary"] == "报告主句：设备出口限制落地"


def test_trace_summary_falls_back_to_trigger_claim_when_headline_empty():
    """B：报告 summary 为空（trigger 无标题）→ 退回 trigger claim（仍非现象/非兜底）。"""

    class R:
        sector = "半导体材料"
        trace_result = {
            "stages": [
                {"kind": "phenomenon", "headline": "今日大幅波动", "claims": []},
                {"kind": "trigger", "headline": "", "claims": ["某部委发布出口管制清单"]},
            ],
            "attribution_status": "insufficient",
        }
        snapshot = {"sector": {"name": "半导体材料", "pct_change": -3.0}}

    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[R()],
    )
    assert chain["children"][0]["trace_summary"] == "某部委发布出口管制清单"


def test_trace_summary_fallback_when_report_summary_empty():
    """B：报告确实无内容（trigger headline/claims 皆空）→ 才用中性兜底文案。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-03",
        review_payload=_review_payload(),
        sector_results=[_sector_result("半导体材料", -3.0, "")],
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


@pytest.mark.asyncio
async def test_save_failed_warning_carries_real_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R17 遗留：`save_failed` 文案须带**真实原因**（业务码/HTTP 状态），不再只报"返回 None"。

    R17 生产事故的排障代价正来自这里：真实原因只在 `node_api_post_business_error` 那条
    **独立**日志里，得交叉 grep 才能定位。现在由 `_post_request` 经 `error_out` 出参回传，
    `save()` 把它并入同一条 warning。
    """
    from aistock_agent.services.attribution_chain import AttributionChainStore

    store = AttributionChainStore()

    async def _fake_post(path, body, *, timeout=None, error_out=None):
        if error_out is not None:
            error_out["stage"] = "business_error"
            error_out["detail"] = "code=500 message=boom"
        return None

    with patch.object(store.node_api, "post", new=_fake_post):
        await store.save(
            "2026-09-03",
            {"date": "2026-09-03", "root": {"type": "market"}, "children": []},
        )
    out = capsys.readouterr().out
    assert "attribution_chain.save_failed" in out
    assert "business_error" in out      # stage
    assert "code=500" in out            # detail


@pytest.mark.asyncio
async def test_post_request_fills_error_out_on_business_error() -> None:
    """`_post_request` 失败时必须把原因写进 `error_out` 出参（成功时**不写**）。"""
    from aistock_agent.services.data_client import HttpClientPool, node_api

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"code": 500, "message": "boom"}

    class _Client:
        async def post(self, *args: object, **kwargs: object) -> object:
            return _Resp()

    with patch.object(HttpClientPool, "get_client", AsyncMock(return_value=_Client())):
        cause: dict[str, object] = {}
        data = await node_api._post_request("/api/internal/attribution-chain", {}, error_out=cause)
    assert data is None
    assert cause["stage"] == "business_error"
    assert "500" in str(cause["detail"])

    # 成功路径不得写出参（调用方据此区分"有原因"与"无原因"）
    class _OkResp(_Resp):
        def json(self) -> dict[str, object]:
            return {"code": 200, "data": {"ok": True}}

    class _OkClient:
        async def post(self, *args: object, **kwargs: object) -> object:
            return _OkResp()

    with patch.object(HttpClientPool, "get_client", AsyncMock(return_value=_OkClient())):
        ok_cause: dict[str, object] = {}
        ok_data = await node_api._post_request(
            "/api/internal/attribution-chain", {}, error_out=ok_cause
        )
    assert ok_data == {"ok": True}
    assert ok_cause == {}


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
    sector_row: dict[str, object] | None = None,
):
    """板块溯源结果（真实 dump 形状 + 快照 sources + 可选报告 attribution_parent/sector_row）。"""
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
    if sector_row is not None:
        R.sector_row = sector_row  # type: ignore[attr-defined]
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
                    _search_source("半导体材料板块库存去化快于预期", url="https://news.example.com/f1"),
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
    sector_row: dict[str, object] | None = None,
):
    """板块溯源结果 + 提取来源/弱标记（SectorTraceRunResult.extraction 的真实形状）。"""
    result = _sector_with_evidence(name, pct, summary, sector_row=sector_row)
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


# --- R14：链板块落「权威名 + ts_code」（消除前端按名匹配不上角色徽） ---

_SNAPSHOT_ROW = {"name": "黄金概念", "ts_code": "885525.TI", "pct_change": -1.87}


@pytest.mark.parametrize("source", ["primary_claim", "candidate_claim", "snapshot"])
def test_child_carries_authoritative_ts_code_and_std_name(source: str) -> None:
    """T1/T2/T3 三条提取路径都带 ts_code + sector_std（取自快照权威行，不是复盘原始名）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("贵金属领跌"),
        sector_results=[
            _sector_with_extraction("贵金属", -1.87, source, sector_row=_SNAPSHOT_ROW)
        ],
    )
    child = chain["children"][0]
    assert child["sector"] == "贵金属"  # 复盘原始名保持现状（向后兼容）
    assert child["ts_code"] == "885525.TI"
    # sector_std 取快照行的权威名（"黄金概念" 剥「概念」后缀 → 小写），不是原始名 "贵金属"
    assert child["sector_std"] == "黄金"


def test_child_std_name_falls_back_to_raw_sector_name() -> None:
    """快照行缺 name → sector_std 回退复盘原始名归一（同前端归一化口径，不编造权威名）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("存储板块 领跌"),
        sector_results=[
            _sector_with_evidence("存储板块 ", -1.87, sector_row={"ts_code": "885525.TI"})
        ],
    )
    child = chain["children"][0]
    assert child["ts_code"] == "885525.TI"
    assert child["sector_std"] == "存储"  # 去空白 + 剥「板块」后缀


def test_child_omits_meta_keys_when_snapshot_row_missing() -> None:
    """无 sector_row / 行内缺 ts_code → 省略 ts_code（不写 null、不崩），sector 仍非空字符串。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("半导体材料领跌"),
        sector_results=[
            _sector_with_evidence("半导体材料", -3.0),
            _sector_with_evidence("存储芯片", -2.0, sector_row={"name": 123}),
        ],
    )
    for child in chain["children"]:
        assert "ts_code" not in child
        assert isinstance(child["sector"], str) and child["sector"]
    assert chain["children"][0]["sector_std"] == "半导体材料"
    assert chain["children"][1]["sector_std"] == "存储芯片"  # 行 name 非字符串 → 回退原始名


def test_child_omits_std_name_for_empty_sector() -> None:
    """板块名与行名都不可用 → 不产 sector_std（空归一化无匹配意义），其余字段照常。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("无板块"),
        sector_results=[_sector_with_evidence("", -3.0, sector_row={})],
    )
    child = chain["children"][0]
    assert child["sector"] == ""
    assert "sector_std" not in child
    assert "ts_code" not in child


def test_child_meta_is_json_friendly() -> None:
    """序列化友好：新增字段只能是 str（不得混入 Pydantic/快照对象）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload_with_trace("黄金概念领跌"),
        sector_results=[
            _sector_with_evidence("黄金概念", -1.87, sector_row=_SNAPSHOT_ROW)
        ],
    )
    child = chain["children"][0]
    assert isinstance(child["ts_code"], str)
    assert isinstance(child["sector_std"], str)
    dumped = json.dumps(chain, ensure_ascii=False)
    assert "885525.TI" in dumped


# --- 2026-09-18：事件准入只收「驱动原因」，拒收「行情综述/现象」（组长口径） ---
#
# 生产实证（2026-09-17 CRO 概念）：事件层被"A股收評|滬指跌0.41%…""今天A股，三大指数
# 集体下跌 - 时间线- 搜狐"这类**行情综述**填充——它们回答不了"为什么动"。口径：宁可
# 漏判（少放）也不把综述当原因；筛完为空即 events=[]（如实交空，不得回退成综述）。

# 综述/现象形态（含生产实证两例 + 简繁 + 英文）
_RECAP_HEADLINES = [
    "A股收評| 滬指跌0.41% 三大指數收跌農業板塊逆勢大漲",
    "今天A股，三大指数集体下跌 - 时间线- 搜狐",
    "半导体材料板块今日收评：主力资金净流出居前",
    "半导体材料板块复盘：午后跌幅扩大",
    "两市成交额跌破万亿，沪指跌0.41%",
    "涨跌家数显示市场情绪转弱，盘面承压",
    "券商板块午评：早盘冲高回落",
    "Closing Bell: S&P 500 falls 0.4% as tech slides",
]

# 驱动原因形态（政策/监管/供需/价格/公司公告/行业事件）
_DRIVING_HEADLINES = [
    "工信部发布光伏制造行业规范条件 推动落后产能退出",
    "商务部对原产于X的进口多晶硅加征关税",
    "某公司公告：拟收购XX股权并复牌",
    "多晶硅价格上涨 供需缺口扩大",
]


@pytest.mark.parametrize("headline", _RECAP_HEADLINES)
def test_is_driving_event_rejects_recap_headlines(headline: str) -> None:
    assert is_driving_event(headline) is False


@pytest.mark.parametrize("headline", _DRIVING_HEADLINES)
def test_is_driving_event_keeps_driving_headlines(headline: str) -> None:
    assert is_driving_event(headline) is True


def test_recap_search_sources_produce_empty_events(capsys: pytest.CaptureFixture[str]) -> None:
    """综述类 headline 不产事件节点（含源标题与板块词命中，仍被准入拦下）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[
                    _search_source("半导体材料板块今日收评：主力资金净流出居前"),
                    _search_source("今天A股，三大指数集体下跌 - 时间线- 搜狐"),
                ],
            )
        ],
    )
    assert chain["children"][0]["events"] == []
    out = capsys.readouterr().out
    # 被拒留痕：结构化日志键 + 原因 + 汇总计数（条数可观测）
    assert "chain_event_rejected_not_driving" in out
    assert "summary_marker" in out
    assert "rejected_not_driving" in out


def test_recap_headline_truncated_in_rejection_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """留痕的 headline 必须截断（长标题不整条进日志）。"""
    long_title = "半导体材料板块今日收评：" + "资金净流出居前" * 10
    assert len(long_title) > 60
    assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence("半导体材料", -3.0, sources=[_search_source(long_title)])
        ],
    )
    out = capsys.readouterr().out
    assert long_title not in out


def test_recap_rejected_does_not_fall_back_to_recap() -> None:
    """全部被拒 → events=[]（如实交空），**不得**回退成综述兜底。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[_search_source("A股收評| 滬指跌0.41% 三大指數收跌農業板塊逆勢大漲")],
            )
        ],
    )
    assert chain["children"][0]["events"] == []


def test_driving_search_source_kept_recap_rejected() -> None:
    """同一板块下：驱动原因保留、综述被拒（互不影响）。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[
                    _search_source("半导体材料板块今日收评：主力资金净流出居前"),
                    _search_source("半导体材料出口管制升级落地", url="https://news.example.com/b"),
                ],
            )
        ],
    )
    events = chain["children"][0]["events"]
    assert [e["headline"] for e in events] == ["半导体材料出口管制升级落地"]


def test_driving_keyword_outranks_plain_headline_in_search_candidates() -> None:
    """正向要求：能回答"为什么动"的事件（公告/政策/价格类）排序靠前。"""
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload(),
        sector_results=[
            _sector_with_evidence(
                "半导体材料",
                -3.0,
                sources=[
                    _search_source("半导体材料板块近期持续承压", url="https://news.example.com/g1"),
                    _search_source("半导体材料公司公告：扩产计划获批", url="https://news.example.com/g2"),
                ],
            )
        ],
    )
    assert [e["ref"] for e in chain["children"][0]["events"]] == [
        "https://news.example.com/g2",
        "https://news.example.com/g1",
    ]


def test_warehouse_event_path_ignores_admission_filter() -> None:
    """回归：中台存量事件（event_id 非空）**不受**准入筛选影响（warehouse 路径逐字不变）。"""
    title = "半导体材料板块今日收评：资金净流出"
    chain = assemble_attribution_chain(
        report_date="2026-09-17",
        review_payload=_review_payload(),
        sector_results=[_sector_with_evidence("半导体材料", -3.0)],
        warehouse_events=[
            _warehouse_event("2026-09-17-abc1234567890", title, keywords=["半导体材料"])
        ],
    )
    assert chain["children"][0]["events"] == [
        {
            "event_id": "2026-09-17-abc1234567890",
            "ref": "event:2026-09-17-abc1234567890",
            "headline": title,
            "source": "warehouse",
        }
    ]

