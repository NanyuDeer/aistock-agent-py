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
    # 避免归档写盘影响测试隔离
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review")
    llm = AsyncMock()
    if isinstance(llm_content, Exception):
        llm.ainvoke.side_effect = llm_content
    else:
        llm.ainvoke.return_value = AIMessage(content=llm_content)
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)
    return mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock())


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
    mocker.patch.object(review_agent, "archive_review")
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
