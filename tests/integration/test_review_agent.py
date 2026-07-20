"""review_agent 集成测试 — 受限 JSON 推理路径

验证：
- 快照先于 LLM 调用被冻结（brief Step 1 verbatim）
- 不再使用 ReAct 模式或工具调用
- 校验失败时返回降级文本且不写缓存
- 有效 JSON 渲染主因果链、备选解释和未解问题
- 缓存命中直接返回
- scheduler 触发时持久化
"""
import copy
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.agents.workers import review as review_agent
from aistock_agent.schemas.market_trace import (
    DominantPhenomenon,
    MarketTraceResult,
    MarketTraceSnapshot,
    ReviewArtifact,
    SourceRecord,
)

# ============================================================================
# 测试 fixtures — SCHEDULER_STATE / TRACE_SNAPSHOT / VALID_TRACE_JSON
# ============================================================================

SCHEDULER_STATE: dict[str, object] = {
    "messages": [],
    "session_id": "test",
    "user_id": None,
    "favorites": [],
    "intent": None,
    "symbol": None,
    "tag_code": None,
    "analysis_reports": {},
    "final_response": None,
    "trigger_source": "scheduler",
    "report_date": "2026-07-17",
}

_CAPTURED_AT = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)
_TRADE_DATE = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _make_source(source_id: str, **overrides: object) -> SourceRecord:
    """构建测试用 SourceRecord，允许覆盖默认字段。"""
    defaults: dict[str, object] = {
        "source_id": source_id,
        "kind": "market_fact",
        "provider": "test",
        "title": source_id,
        "content": "test content",
        "url": None,
        "occurred_at": _TRADE_DATE,
        "captured_at": _CAPTURED_AT,
        "source_level": "market_data",
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)  # type: ignore[arg-type]


TRACE_SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260717",
    trade_date="2026-07-17",
    captured_at=_CAPTURED_AT,
    a_share={
        "sectors": {
            "top_gainers": [{"name": "半导体"}],
            "top_losers": [{"name": "房地产"}],
            "top_inflows": [],
            "top_outflows": [],
        },
    },
    sources={
        "INDEX_000001_SH": _make_source(
            "INDEX_000001_SH",
            provider="tushare:index_daily",
            title="上证指数",
            content="close=3200.0, pct_chg=0.5",
        ),
        "GLOBAL_001": _make_source(
            "GLOBAL_001",
            provider="yfinance",
            title="标普500",
            content="price=5500.0, change_pct=0.36",
        ),
        "NEWS_001": _make_source(
            "NEWS_001",
            kind="event_evidence",
            provider="cls",
            title="央行宣布降准",
            content="中国人民银行决定下调存款准备金率0.5个百分点",
            url="https://www.cls.cn/news/1",
            source_level="reporting",
        ),
        "SEARCH_001": _make_source(
            "SEARCH_001",
            kind="event_evidence",
            provider="tavily",
            title="美联储维持利率不变",
            content="美联储在最新议息会议上决定维持联邦基金利率目标区间不变",
            url="https://example.com/fed",
            source_level="reporting",
        ),
    },
    missing_fields=[],
    dominant_phenomenon=DominantPhenomenon(
        kind="broad_rally",
        summary="多个核心指数同步上涨，市场广度偏强",
        fact_ids=["INDEX_000001_SH"],
        score=3,
    ),
)


def _primary_chain_nodes() -> list[dict[str, object]]:
    """主因候选的 6 阶段因果链节点。"""
    return [
        {"stage": "structural_root", "claim": "国内货币政策宽松周期", "evidence_ids": ["NEWS_001"]},
        {"stage": "trigger", "claim": "央行宣布降准0.5个百分点", "evidence_ids": ["NEWS_001"]},
        {"stage": "transmission", "claim": "银行间流动性宽松传导至权益", "evidence_ids": ["NEWS_001"]},
        {"stage": "exposure", "claim": "金融板块直接受益", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "repricing", "claim": "市场情绪回暖", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "observable_result", "claim": "上证指数上涨0.5%", "evidence_ids": ["INDEX_000001_SH"]},
    ]


def _alternative_chain_nodes() -> list[dict[str, object]]:
    """备选候选的 6 阶段因果链节点。"""
    return [
        {"stage": "structural_root", "claim": "美联储维持利率", "evidence_ids": ["SEARCH_001"]},
        {"stage": "trigger", "claim": "全球流动性宽松预期", "evidence_ids": ["GLOBAL_001"]},
        {"stage": "transmission", "claim": "外资流入新兴市场", "evidence_ids": ["GLOBAL_001"]},
        {"stage": "exposure", "claim": "北向资金净流入", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "repricing", "claim": "权重股估值抬升", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "observable_result", "claim": "上证指数上涨0.5%", "evidence_ids": ["INDEX_000001_SH"]},
    ]


VALID_TRACE_DICT: dict[str, object] = {
    "schema_version": "1.0",
    "dominant_phenomenon": {
        "kind": "broad_rally",
        "summary": "多个核心指数同步上涨，市场广度偏强",
        "fact_ids": ["INDEX_000001_SH"],
        "score": 3,
    },
    "candidates": [
        {
            "id": "global_risk_liquidity",
            "category": "global_risk_liquidity",
            "status": "weak",
            "verdict": "全球风险偏好改善但非主因",
            "chain": {"nodes": _alternative_chain_nodes()},
            "supporting_evidence_ids": ["GLOBAL_001", "SEARCH_001"],
            "counter_evidence_ids": [],
        },
        {
            "id": "domestic_macro_policy",
            "category": "domestic_macro_policy",
            "status": "supported",
            "verdict": "央行降准释放流动性是主因",
            "chain": {"nodes": _primary_chain_nodes()},
            "supporting_evidence_ids": ["NEWS_001", "INDEX_000001_SH"],
            "counter_evidence_ids": [],
        },
        {
            "id": "industry_technology_supply",
            "category": "industry_technology_supply",
            "status": "insufficient",
            "verdict": "无明确产业供给冲击",
            "chain": None,
            "supporting_evidence_ids": [],
            "counter_evidence_ids": [],
        },
        {
            "id": "market_positioning_liquidity",
            "category": "market_positioning_liquidity",
            "status": "rejected",
            "verdict": "市场定位与流动性非独立驱动因素",
            "chain": None,
            "supporting_evidence_ids": [],
            "counter_evidence_ids": ["INDEX_000001_SH"],
        },
    ],
    "primary_chain_id": "domestic_macro_policy",
    "alternative_chain_id": "global_risk_liquidity",
    "confidence": "high",
    "unresolved_questions": ["降准对银行净息差的长期影响尚不明确"],
}

VALID_TRACE_JSON = json.dumps(VALID_TRACE_DICT, ensure_ascii=False)


def _make_cached_artifact(markdown: str) -> ReviewArtifact:
    """构建缓存命中测试用的最小合法 ReviewArtifact。"""
    return ReviewArtifact(
        schema_version="1.0",
        snapshot=TRACE_SNAPSHOT,
        trace=MarketTraceResult.model_validate(VALID_TRACE_DICT),
        markdown=markdown,
        trace_summary="cached summary",
        sectors=["半导体"],
    )


def _trace_json_with(modifier: object) -> str:
    """深拷贝 VALID_TRACE_DICT，应用 modifier(trace_dict)，返回 JSON 字符串。"""
    trace = copy.deepcopy(VALID_TRACE_DICT)
    if callable(modifier):
        modifier(trace)
    return json.dumps(trace, ensure_ascii=False)


def _patch_snapshot_and_llm(mocker, llm_content: str | Exception) -> object:
    """统一 mock build_market_trace_snapshot + get_deep_think + archive，返回 mock_set_cache。"""
    async def build_snapshot(_date: str):
        return TRACE_SNAPSHOT

    mocker.patch.object(review_agent, "build_market_trace_snapshot", build_snapshot)
    # 避免归档写盘影响测试隔离；archive_review 和 set_cached_review 必须返回 True，
    # 否则严格失败顺序会返回降级文本，干扰 LLM/校验相关测试断言。
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    llm = AsyncMock()
    if isinstance(llm_content, Exception):
        llm.ainvoke.side_effect = llm_content
    else:
        llm.ainvoke.return_value = AIMessage(content=llm_content)
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)
    return mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))


# ============================================================================
# Step 1 verbatim 测试 — 快照先于 LLM，单次结构化推理
# ============================================================================


@pytest.mark.asyncio
async def test_review_freezes_snapshot_before_single_structured_llm_call(mocker):
    order: list[str] = []

    async def build_snapshot(_date: str):
        order.append("snapshot")
        return TRACE_SNAPSHOT

    mocker.patch.object(review_agent, "build_market_trace_snapshot", build_snapshot)
    # 避免归档写盘影响测试隔离
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    # set_cached_review 必须返回 True，否则严格失败顺序会返回降级文本
    mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))
    llm = AsyncMock()
    llm.ainvoke.side_effect = lambda _messages: order.append("llm") or AIMessage(content=VALID_TRACE_JSON)
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)

    result = await review_agent.run(SCHEDULER_STATE)

    assert order == ["snapshot", "llm"]
    assert "主因果链" in result["final_response"]


# ============================================================================
# 有效 JSON — 渲染主因果链、备选解释和未解问题
# ============================================================================


@pytest.mark.asyncio
async def test_review_valid_trace_renders_primary_alternative_and_unresolved(mocker):
    """有效 JSON 返回主因果链、备选解释和未解问题。"""
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON)

    result = await review_agent.run(SCHEDULER_STATE)

    assert "主因果链" in result["final_response"]
    assert "备选解释" in result["final_response"]
    assert "未解问题" in result["final_response"]
    assert "降准对银行净息差的长期影响尚不明确" in result["final_response"]
    mock_set_cache.assert_called_once()


# ============================================================================
# 校验失败 → 降级文本，不写缓存
# ============================================================================


@pytest.mark.asyncio
async def test_review_returns_degraded_when_trace_references_unknown_source_id(mocker):
    """模型输出引用了不存在的 source_id → 降级文本，不写缓存。"""

    def _modify_unknown_source(trace: dict) -> None:
        for c in trace["candidates"]:
            if c["id"] == "domestic_macro_policy":
                c["supporting_evidence_ids"].append("NONEXISTENT_001")

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_unknown_source))

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_returns_degraded_when_primary_chain_misses_stages(mocker):
    """主因 chain 缺少阶段 → 降级文本，不写缓存。"""

    def _modify_missing_stages(trace: dict) -> None:
        for c in trace["candidates"]:
            if c["id"] == "domestic_macro_policy":
                c["chain"]["nodes"].pop()  # 删除 observable_result

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_missing_stages))

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_returns_degraded_when_candidate_categories_incomplete(mocker):
    """候选类别不全（只有 3 条）→ 降级文本，不写缓存。"""

    def _modify_remove_candidate(trace: dict) -> None:
        trace["candidates"] = [
            c for c in trace["candidates"] if c["id"] != "industry_technology_supply"
        ]

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_remove_candidate))

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_returns_degraded_when_primary_equals_alternative(mocker):
    """主备同 ID → 降级文本，不写缓存。"""

    def _modify_same_id(trace: dict) -> None:
        trace["alternative_chain_id"] = trace["primary_chain_id"]

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_same_id))

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_returns_degraded_when_llm_raises(mocker):
    """LLM 调用异常 → 降级文本，不写缓存。"""
    mock_set_cache = _patch_snapshot_and_llm(mocker, Exception("LLM down"))

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


# ============================================================================
# 缓存命中 — 直接返回，不调用 LLM
# ============================================================================


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=_make_cached_artifact("cached review").model_dump(mode="json"),
)
async def test_review_run_cache_hit(_mock_cache):
    """缓存命中：直接返回缓存内容，不调用 LLM。"""
    state = {
        "messages": [],
        "session_id": "test",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "report_date": "2026-07-17",
    }
    result = await review_agent.run(state)
    assert result["final_response"] == "cached review"


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=_make_cached_artifact("cached scheduler review").model_dump(mode="json"),
)
async def test_cached_scheduler_review_is_still_persisted(_mock_cache):
    """scheduler 触发 + 缓存命中：仍需按 schema v2 持久化（供下游读取）。"""
    with patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock()) as save:
        state = {
            "messages": [],
            "session_id": "test",
            "user_id": None,
            "favorites": [],
            "intent": None,
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
            "trigger_source": "scheduler",
            "report_date": "2026-07-17",
        }
        result = await review_agent.run(state)

    assert result["final_response"] == "cached scheduler review"
    save.assert_awaited_once()
    kwargs = save.await_args.kwargs
    assert kwargs["report_type"] == "review"
    assert kwargs["report_date"] == "2026-07-17"
    assert kwargs["content"]["schema_version"] == "2.0"
    assert kwargs["content"]["display_report"]["details"] == "cached scheduler review"


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=_make_cached_artifact("cached manual review").model_dump(mode="json"),
)
async def test_cached_manual_review_is_not_persisted(_mock_cache):
    """手动触发（无 trigger_source='scheduler'）+ 缓存命中：不调用持久化。"""
    with patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock()) as save:
        state = {
            "messages": [],
            "session_id": "test",
            "user_id": None,
            "favorites": [],
            "intent": None,
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
            "report_date": "2026-07-17",
        }
        result = await review_agent.run(state)

    assert result["final_response"] == "cached manual review"
    save.assert_not_awaited()


# ============================================================================
# scheduler 持久化失败不应影响复盘正常返回
# ============================================================================


@pytest.mark.asyncio
async def test_scheduler_persist_failure_keeps_review_response(mocker):
    """scheduler 持久化失败不应影响复盘正常返回（降级吞掉异常）。"""
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON)
    mocker.patch.object(review_agent, "archive_review")

    with patch.object(
        review_agent.node_api,
        "save_analysis_report",
        new=AsyncMock(side_effect=Exception("DB down")),
    ) as save:
        result = await review_agent.run(SCHEDULER_STATE)

    assert "主因果链" in result["final_response"]
    save.assert_awaited_once()


# ============================================================================
# Step 3 额外断言 — 主因选择与证据不足场景
# ============================================================================


@pytest.mark.asyncio
async def test_review_primary_selects_supported_when_global_risk_rejected(mocker):
    """global_risk rejected + domestic_macro supported → 主因是 domestic_macro。"""
    def _reject_global_risk(trace: dict) -> None:
        for c in trace["candidates"]:
            if c["id"] == "global_risk_liquidity":
                c["status"] = "rejected"
                c["chain"] = None
        trace["alternative_chain_id"] = None

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_reject_global_risk))

    result = await review_agent.run(SCHEDULER_STATE)

    assert "domestic_macro_policy" in result["final_response"]
    assert "央行降准释放流动性是主因" in result["final_response"]
    mock_set_cache.assert_called_once()


@pytest.mark.asyncio
async def test_review_all_insufficient_reports_evidence_insufficient(mocker):
    """所有候选标记 insufficient → 报告明确'证据不足，未确认主因'，不选择最像的解释。"""
    def _all_insufficient(trace: dict) -> None:
        for c in trace["candidates"]:
            c["status"] = "insufficient"
            c["chain"] = None
        trace["primary_chain_id"] = None
        trace["alternative_chain_id"] = None

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_all_insufficient))

    result = await review_agent.run(SCHEDULER_STATE)

    assert "证据不足" in result["final_response"] or "未确认主因" in result["final_response"]
    mock_set_cache.assert_called_once()


# ============================================================================
# Task 5 review 修复 — 强化结构化归因校验
# ============================================================================


@pytest.mark.asyncio
async def test_review_degraded_when_trace_dominant_phenomenon_mismatches_snapshot(mocker):
    """trace.dominant_phenomenon.kind 与 snapshot.dominant_phenomenon.kind 不一致 → 降级。"""
    def _modify_dp_kind(trace: dict) -> None:
        # snapshot 是 broad_rally，把 trace 改成 broad_decline
        if trace.get("dominant_phenomenon"):
            trace["dominant_phenomenon"]["kind"] = "broad_decline"

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_dp_kind))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_dominant_phenomenon_fact_ids_not_in_sources(mocker):
    """trace.dominant_phenomenon.fact_ids 必须全部存在于 snapshot.sources。"""
    def _modify_dp_fact_id(trace: dict) -> None:
        if trace.get("dominant_phenomenon"):
            # 引用不存在的 source_id
            trace["dominant_phenomenon"]["fact_ids"] = ["NONEXISTENT_SOURCE"]

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_dp_fact_id))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_chain_node_has_empty_evidence_ids(mocker):
    """每个因果节点的 evidence_ids 不得为空。"""
    def _modify_empty_node_evidence(trace: dict) -> None:
        for c in trace["candidates"]:
            if c["id"] == "domestic_macro_policy" and c.get("chain"):
                # 把 trigger 节点的证据清空
                for node in c["chain"]["nodes"]:
                    if node["stage"] == "trigger":
                        node["evidence_ids"] = []

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_empty_node_evidence))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_observable_result_lacks_market_fact_evidence(mocker):
    """observable_result 节点必须至少引用一个 kind=market_fact 的事实。

    场景：observable_result 只引用 NEWS_001（event_evidence），没有 market_fact。
    """
    def _modify_or_no_market_fact(trace: dict) -> None:
        for c in trace["candidates"]:
            if c["id"] == "domestic_macro_policy" and c.get("chain"):
                for node in c["chain"]["nodes"]:
                    if node["stage"] == "observable_result":
                        # 只引用事件证据（NEWS_001 是 event_evidence）
                        node["evidence_ids"] = ["NEWS_001"]

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_or_no_market_fact))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_primary_null_but_alternative_invalid(mocker):
    """primary_chain_id=null 时仍需校验非空 alternative 的 ID、status、chain 和 6 阶段顺序。

    场景：所有 supported 候选被改成 insufficient，primary=null；
    alternative 仍指向原 global_risk_liquidity（status=weak），但其 chain 缺一个阶段。
    修复前：validate_chain_stages 在 primary=null 时直接 return，不校验 alternative；
    修复后：alternative 的 chain 也必须通过 6 阶段校验。
    """
    def _modify_primary_null_bad_alt(trace: dict) -> None:
        # 让所有候选变 insufficient（无 supported），primary 必须为 null
        for c in trace["candidates"]:
            c["status"] = "insufficient"
            c["chain"] = None
        # 但仍保留 global_risk_liquidity 的 chain（weak 链），并截掉一个阶段
        for c in trace["candidates"]:
            if c["id"] == "global_risk_liquidity":
                c["status"] = "weak"
                c["chain"] = {"nodes": _alternative_chain_nodes()[:-1]}  # 缺 observable_result
        trace["primary_chain_id"] = None
        trace["alternative_chain_id"] = "global_risk_liquidity"

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_primary_null_bad_alt))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_primary_null_alternative_valid_succeeds(mocker):
    """primary=null + alternative 合法（6 阶段完整、status=weak）→ 不降级。"""
    def _modify_primary_null_valid_alt(trace: dict) -> None:
        for c in trace["candidates"]:
            if c["id"] == "domestic_macro_policy":
                c["status"] = "insufficient"
                c["chain"] = None
        trace["primary_chain_id"] = None
        # alternative_chain_id 已经是 global_risk_liquidity (weak, 完整 6 阶段)
        # 保留不变

    mock_set_cache = _patch_snapshot_and_llm(
        mocker, _trace_json_with(_modify_primary_null_valid_alt)
    )
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] != review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_called_once()


@pytest.mark.asyncio
async def test_review_degraded_when_primary_null_and_alternative_status_invalid(mocker):
    """primary=null + alternative 指向 rejected 候选（6 阶段完整）→ 必须降级。

    场景：所有 supported 候选改成 rejected/insufficient，primary=null；
    alternative 仍指向 global_risk_liquidity，但其 status 改成 rejected，
    chain 保留完整 6 阶段。修复前：validate_selected_chain_ids 在 primary=null
    时直接 return，不校验 alternative 的 status；validate_chain_stages 只校验
    阶段顺序，不校验 status → 错误通过。修复后：无论 primary 是否为 null，
    非空 alternative 的 status 必须是 supported 或 weak。
    """
    def _modify_primary_null_rejected_alt(trace: dict) -> None:
        # 让所有候选变 insufficient（无 supported），primary 必须为 null
        for c in trace["candidates"]:
            c["status"] = "insufficient"
            c["chain"] = None
        # global_risk_liquidity 改成 rejected 但保留完整 6 阶段 chain
        for c in trace["candidates"]:
            if c["id"] == "global_risk_liquidity":
                c["status"] = "rejected"
                c["chain"] = {"nodes": _alternative_chain_nodes()}  # 完整 6 阶段
        trace["primary_chain_id"] = None
        trace["alternative_chain_id"] = "global_risk_liquidity"

    mock_set_cache = _patch_snapshot_and_llm(
        mocker, _trace_json_with(_modify_primary_null_rejected_alt)
    )
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_dominant_phenomenon_fact_ids_swap_to_unrelated_existing_source(
    mocker,
):
    """trace.dominant_phenomenon.fact_ids 篡改为存在但无关的 source_id → 必须降级。

    场景：snapshot.dominant_phenomenon.fact_ids=["INDEX_000001_SH"]（市场事实），
    模型把 trace.dominant_phenomenon.fact_ids 改成 ["NEWS_001"]（存在但属于事件证据，
    不是主导现象绑定的市场事实）。修复前：只校验 fact_ids 存在于 snapshot.sources，
    NEWS_001 存在 → 通过；修复后：trace.dominant_phenomenon.fact_ids 必须与
    snapshot.dominant_phenomenon.fact_ids 完全一致（顺序无关），不允许篡改冻结事实。
    """
    def _modify_dp_swap_fact_id(trace: dict) -> None:
        if trace.get("dominant_phenomenon"):
            # 把 fact_ids 从 ["INDEX_000001_SH"] 改成 ["NEWS_001"]（存在但无关）
            trace["dominant_phenomenon"]["fact_ids"] = ["NEWS_001"]

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_dp_swap_fact_id))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_dominant_phenomenon_score_swapped(mocker):
    """trace.dominant_phenomenon.score 与 snapshot 不一致 → 必须降级。

    场景：snapshot.dominant_phenomenon.score=3，模型把 trace 的 score 改成 5。
    修复前：只校验 kind 和 fact_ids 存在性，score 不校验 → 通过；
    修复后：score 必须与 snapshot 完全一致，禁止篡改冻结评分。
    """
    def _modify_dp_score(trace: dict) -> None:
        if trace.get("dominant_phenomenon"):
            trace["dominant_phenomenon"]["score"] = 5  # snapshot 是 3

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_modify_dp_score))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


# ============================================================================
# Task 5 review 修复 — 缓存命中后重新执行语义校验 + 校验缓存日期
# ============================================================================


@pytest.mark.asyncio
async def test_cache_hit_with_mismatched_date_falls_back_to_fresh_path(mocker):
    """缓存日期与快照日期不一致 → 视为未命中，走完整路径。

    场景：缓存里存的 snapshot.trade_date="2026-07-16"，
    但本次 report_date="2026-07-17"。修复前：直接返回缓存 markdown；
    修复后：必须降级或走完整路径，不能把旧日期快照当作今日报告返回。
    """
    stale_snapshot = MarketTraceSnapshot(
        snapshot_id="trace-20260716",
        trade_date="2026-07-16",  # 与 report_date 不一致
        captured_at=_CAPTURED_AT,
        a_share={
            "sectors": {
                "top_gainers": [{"name": "半导体"}],
                "top_losers": [],
                "top_inflows": [],
                "top_outflows": [],
            },
        },
        sources={
            "INDEX_000001_SH": _make_source(
                "INDEX_000001_SH",
                provider="tushare:index_daily",
                title="上证指数",
                content="close=3200.0, pct_chg=0.5",
            ),
        },
        missing_fields=[],
        dominant_phenomenon=DominantPhenomenon(
            kind="broad_rally",
            summary="多个核心指数同步上涨",
            fact_ids=["INDEX_000001_SH"],
            score=3,
        ),
    )
    stale_artifact = ReviewArtifact(
        schema_version="1.0",
        snapshot=stale_snapshot,
        trace=MarketTraceResult.model_validate(VALID_TRACE_DICT),
        markdown="stale cached review",
        trace_summary="stale summary",
        sectors=["半导体"],
    )

    # 缓存命中返回 stale artifact
    mocker.patch.object(
        review_agent,
        "get_cached_review",
        new=AsyncMock(return_value=stale_artifact.model_dump(mode="json")),
    )
    # 同时 mock fresh path，确保走完整路径
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON)

    state = {**SCHEDULER_STATE, "report_date": "2026-07-17"}
    result = await review_agent.run(state)

    # 不能返回 stale 内容
    assert result["final_response"] != "stale cached review"
    # 应当走完整路径并写入新缓存
    mock_set_cache.assert_called_once()


@pytest.mark.asyncio
async def test_cache_hit_with_invalid_trace_semantics_falls_back(mocker):
    """缓存中的 trace 语义不合法（如 dominant_phenomenon 与 snapshot 不一致）→ 视为未命中。

    修复前：缓存命中只做 ReviewArtifact.model_validate，不再做跨对象校验；
    修复后：必须重新执行 validate_trace_against_snapshot。
    """
    # 构造一个能通过 model_validate 但语义不合法的 artifact
    bad_trace_dict = copy.deepcopy(VALID_TRACE_DICT)
    # dominant_phenomenon.kind 与 snapshot 的 broad_rally 不一致 → 必须降级
    bad_trace_dict["dominant_phenomenon"]["kind"] = "broad_decline"
    bad_artifact = ReviewArtifact(
        schema_version="1.0",
        snapshot=TRACE_SNAPSHOT,
        trace=MarketTraceResult.model_validate(bad_trace_dict),
        markdown="bad cached review",
        trace_summary="bad summary",
        sectors=["半导体"],
    )

    mocker.patch.object(
        review_agent,
        "get_cached_review",
        new=AsyncMock(return_value=bad_artifact.model_dump(mode="json")),
    )
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON)

    result = await review_agent.run(SCHEDULER_STATE)

    # 不能返回 bad 内容，应当走完整路径
    assert result["final_response"] != "bad cached review"
    mock_set_cache.assert_called_once()


# ============================================================================
# Task 5 review 修复 — 严格失败顺序：snapshot -> facts -> LLM -> validation
#                                       -> markdown archive -> Redis -> DB
# 任何步骤失败时，后续步骤不得执行。
# ============================================================================


@pytest.mark.asyncio
async def test_facts_archive_failure_blocks_llm_and_downstream(mocker):
    """facts 归档失败 → 不调用 LLM，不写 Markdown / Redis / DB。"""
    order: list[str] = []

    async def build_snapshot(_date: str):
        order.append("snapshot")
        return TRACE_SNAPSHOT

    mocker.patch.object(review_agent, "build_market_trace_snapshot", build_snapshot)
    # facts 归档抛异常
    mocker.patch.object(
        review_agent,
        "archive_market_trace_snapshot",
        side_effect=OSError("disk full"),
    )
    # 这些后续步骤都不应被调用
    mock_archive_review = mocker.patch.object(review_agent, "archive_review")
    mock_get_llm = mocker.patch.object(review_agent, "get_deep_think")
    mock_set_cache = mocker.patch.object(
        review_agent, "set_cached_review", new=AsyncMock()
    )
    mock_save = mocker.patch.object(
        review_agent.node_api, "save_analysis_report", new=AsyncMock()
    )

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    assert order == ["snapshot"]
    mock_get_llm.assert_not_called()
    mock_archive_review.assert_not_called()
    mock_set_cache.assert_not_called()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_markdown_archive_failure_blocks_redis_and_db(mocker):
    """Markdown 归档失败 → 不写 Redis / DB。"""
    order: list[str] = []

    async def build_snapshot(_date: str):
        order.append("snapshot")
        return TRACE_SNAPSHOT

    mocker.patch.object(review_agent, "build_market_trace_snapshot", build_snapshot)
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    llm = AsyncMock()
    llm.ainvoke.side_effect = lambda _msgs: (
        order.append("llm") or AIMessage(content=VALID_TRACE_JSON)
    )
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)
    # Markdown 归档返回 False（失败）
    mocker.patch.object(review_agent, "archive_review", return_value=False)
    mock_set_cache = mocker.patch.object(
        review_agent, "set_cached_review", new=AsyncMock()
    )
    mock_save = mocker.patch.object(
        review_agent.node_api, "save_analysis_report", new=AsyncMock()
    )

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    assert order == ["snapshot", "llm"]
    mock_set_cache.assert_not_called()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_cache_failure_blocks_db_persistence(mocker):
    """Redis 缓存写入失败 → 不写 DB。"""
    order: list[str] = []

    async def build_snapshot(_date: str):
        order.append("snapshot")
        return TRACE_SNAPSHOT

    mocker.patch.object(review_agent, "build_market_trace_snapshot", build_snapshot)
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    llm = AsyncMock()
    llm.ainvoke.side_effect = lambda _msgs: (
        order.append("llm") or AIMessage(content=VALID_TRACE_JSON)
    )
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)
    # Markdown 归档成功
    mocker.patch.object(
        review_agent,
        "archive_review",
        side_effect=lambda _md, _sid: order.append("markdown_archive") or True,
    )
    # Redis 缓存写入返回 False（失败）
    mock_set_cache = mocker.patch.object(
        review_agent,
        "set_cached_review",
        new=AsyncMock(return_value=False),
    )
    mock_save = mocker.patch.object(
        review_agent.node_api, "save_analysis_report", new=AsyncMock()
    )

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    assert order == ["snapshot", "llm", "markdown_archive"]
    mock_set_cache.assert_awaited_once()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_success_sequence_is_snapshot_facts_llm_markdown_redis_db(mocker):
    """完整成功时序：snapshot → facts archive → LLM → markdown archive → Redis → DB。"""
    order: list[str] = []

    async def build_snapshot(_date: str):
        order.append("snapshot")
        return TRACE_SNAPSHOT

    mocker.patch.object(review_agent, "build_market_trace_snapshot", build_snapshot)
    mocker.patch.object(
        review_agent,
        "archive_market_trace_snapshot",
        side_effect=lambda _snap: order.append("facts_archive"),
    )
    llm = AsyncMock()
    llm.ainvoke.side_effect = lambda _msgs: (
        order.append("llm") or AIMessage(content=VALID_TRACE_JSON)
    )
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)
    mocker.patch.object(
        review_agent,
        "archive_review",
        side_effect=lambda _md, _sid: order.append("markdown_archive") or True,
    )

    async def set_cache(_date: str, _artifact: dict) -> bool:
        order.append("redis_cache")
        return True

    mocker.patch.object(review_agent, "set_cached_review", new=set_cache)

    async def save_report(*_args, **_kwargs):
        order.append("db_persist")

    mocker.patch.object(review_agent.node_api, "save_analysis_report", new=save_report)

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] != review_agent.DEGRADED_RESPONSE
    assert order == [
        "snapshot",
        "facts_archive",
        "llm",
        "markdown_archive",
        "redis_cache",
        "db_persist",
    ]
