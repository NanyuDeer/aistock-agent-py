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
    MarketTraceResult,
    MarketTraceSnapshot,
    MorningEvent,
    MorningForecast,
    MorningSectorView,
    ReviewArtifact,
    SourceRecord,
)
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon

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


_A_SHARE: dict[str, object] = {
    "indexes": {
        "SH000001": {"ts_code": "000001.SH", "pct_chg": 1.2},
        "SZ399001": {"ts_code": "399001.SZ", "pct_chg": 1.5},
        "SZ399006": {"ts_code": "399006.SZ", "pct_chg": 1.8},
        "SH000300": {"ts_code": "000300.SH", "pct_chg": 1.0},
        "SH000905": {"ts_code": "000905.SH", "pct_chg": 0.9},
        "SH000852": {"ts_code": "000852.SH", "pct_chg": 1.1},
    },
    "breadth": {"advance_ratio": 0.75, "total_count": 5000, "decline_count": 1000},
    "turnover": {"change_pct": 15.0},
    "limits": {"up_count": 50, "down_count": 10, "broken_count": 5, "highest_board": 3},
    "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
    "sectors": {
        "top_gainers": [{"name": "半导体"}],
        "top_losers": [{"name": "房地产"}],
        "top_inflows": [],
        "top_outflows": [],
    },
}

_SOURCES = {
    "INDEX_000001_SH": _make_source(
        "INDEX_000001_SH",
        provider="tushare:index_daily",
        title="上证指数",
        content="close=3200.0, pct_chg=0.5",
    ),
    "INDEX_399001_SZ": _make_source("INDEX_399001_SZ"),
    "INDEX_399006_SZ": _make_source("INDEX_399006_SZ"),
    "INDEX_000300_SH": _make_source("INDEX_000300_SH"),
    "INDEX_000905_SH": _make_source("INDEX_000905_SH"),
    "INDEX_000852_SH": _make_source("INDEX_000852_SH"),
    "BREADTH_ALL": _make_source("BREADTH_ALL"),
    "TURNOVER_ALL": _make_source("TURNOVER_ALL"),
    "LIMITS_ALL": _make_source("LIMITS_ALL"),
    "MAIN_FORCE_ALL": _make_source("MAIN_FORCE_ALL"),
    "SECTORS_ALL": _make_source("SECTORS_ALL"),
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
}

TRACE_SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260717",
    trade_date="2026-07-17",
    captured_at=_CAPTURED_AT,
    a_share=_A_SHARE,
    sources=_SOURCES,
    missing_fields=[],
    phenomenon_discovery=discover_market_phenomenon(_A_SHARE, _SOURCES, _CAPTURED_AT, []),
)


def _primary_chain_nodes() -> list[dict[str, object]]:
    """主因候选的 6 阶段因果链节点。"""
    return [
        {"stage": "structural_root", "claim": "国内货币政策宽松周期", "evidence_ids": ["NEWS_001"]},
        {"stage": "trigger", "claim": "央行宣布降准0.5个百分点", "evidence_ids": ["NEWS_001"]},
        {
            "stage": "transmission",
            "claim": "银行间流动性宽松传导至权益",
            "evidence_ids": ["NEWS_001"],
        },
        {"stage": "exposure", "claim": "金融板块直接受益", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "repricing", "claim": "市场情绪回暖", "evidence_ids": ["INDEX_000001_SH"]},
        {
            "stage": "observable_result",
            "claim": "上证指数上涨0.5%",
            "evidence_ids": ["INDEX_000001_SH"],
        },
    ]


def _alternative_chain_nodes() -> list[dict[str, object]]:
    """备选候选的 6 阶段因果链节点。"""
    return [
        {"stage": "structural_root", "claim": "美联储维持利率", "evidence_ids": ["SEARCH_001"]},
        {"stage": "trigger", "claim": "全球流动性宽松预期", "evidence_ids": ["GLOBAL_001"]},
        {"stage": "transmission", "claim": "外资流入新兴市场", "evidence_ids": ["GLOBAL_001"]},
        {"stage": "exposure", "claim": "北向资金净流入", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "repricing", "claim": "权重股估值抬升", "evidence_ids": ["INDEX_000001_SH"]},
        {
            "stage": "observable_result",
            "claim": "上证指数上涨0.5%",
            "evidence_ids": ["INDEX_000001_SH"],
        },
    ]


VALID_TRACE_DICT: dict[str, object] = {
    "schema_version": "1.1",
    "attribution_status": "confirmed",
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
        schema_version="1.1",
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


def test_validate_snapshot_discovery_rejects_tampered_frozen_result() -> None:
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    tampered = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={"primary": primary.model_copy(update={"summary": "篡改摘要"})}
    )
    snapshot = TRACE_SNAPSHOT.model_copy(update={"phenomenon_discovery": tampered})

    with pytest.raises(ValueError, match="discovery"):
        review_agent.validate_snapshot_discovery(snapshot)


def _snapshot_with_mismatched_source_id() -> MarketTraceSnapshot:
    sources = dict(TRACE_SNAPSHOT.sources)
    sources["NEWS_001"] = sources["NEWS_001"].model_copy(update={"source_id": "NEWS_MISMATCH"})
    return TRACE_SNAPSHOT.model_copy(update={"sources": sources})


def test_validate_snapshot_discovery_rejects_source_map_key_mismatch() -> None:
    with pytest.raises(ValueError, match="source map key mismatch"):
        review_agent.validate_snapshot_discovery(_snapshot_with_mismatched_source_id())


def test_validate_empty_trace_rejects_noncanonical_confidence_and_questions() -> None:
    missing_fields = ["a_share.indexes"]
    discovery = discover_market_phenomenon({}, {}, _CAPTURED_AT, missing_fields)
    snapshot = TRACE_SNAPSHOT.model_copy(
        update={
            "a_share": {},
            "sources": {},
            "missing_fields": missing_fields,
            "phenomenon_discovery": discovery,
        }
    )
    trace = MarketTraceResult(
        schema_version="1.1",
        attribution_status="insufficient",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="high",
        unresolved_questions=["篡改问题"],
    )

    with pytest.raises(ValueError, match="empty trace"):
        review_agent.validate_trace_against_snapshot(trace, snapshot)


def test_review_prompt_freezes_discovery_and_schema_1_1_rules() -> None:
    prompt = review_agent.REVIEW_PROMPT
    assert 'schema_version: "1.1"' in prompt
    assert "primary 是唯一归因对象" in prompt
    assert "ready 不等于确认" in prompt
    assert "occurred_at" in prompt
    assert "concurrent_phenomena" in prompt


def test_confirmed_trace_requires_ready_causal_evidence() -> None:
    sources = dict(TRACE_SNAPSHOT.sources)
    sources["NEWS_001"] = sources["NEWS_001"].model_copy(update={"url": None})
    sources["SEARCH_001"] = sources["SEARCH_001"].model_copy(update={"url": None})
    discovery = discover_market_phenomenon(_A_SHARE, sources, _CAPTURED_AT, [])
    snapshot = TRACE_SNAPSHOT.model_copy(
        update={"sources": sources, "phenomenon_discovery": discovery}
    )

    with pytest.raises(ValueError, match="confirmed"):
        review_agent.validate_trace_against_snapshot(
            MarketTraceResult.model_validate(VALID_TRACE_DICT), snapshot
        )


def test_confirmed_trigger_requires_traceable_event_evidence() -> None:
    trace_dict = copy.deepcopy(VALID_TRACE_DICT)
    primary = next(
        candidate
        for candidate in trace_dict["candidates"]
        if candidate["id"] == "domestic_macro_policy"
    )
    trigger = next(node for node in primary["chain"]["nodes"] if node["stage"] == "trigger")
    trigger["evidence_ids"] = ["GLOBAL_001"]

    with pytest.raises(ValueError, match="trigger"):
        review_agent.validate_trace_against_snapshot(
            MarketTraceResult.model_validate(trace_dict), TRACE_SNAPSHOT
        )


def test_observable_result_must_reference_primary_phenomenon_fact() -> None:
    trace_dict = copy.deepcopy(VALID_TRACE_DICT)
    primary = next(
        candidate
        for candidate in trace_dict["candidates"]
        if candidate["id"] == "domestic_macro_policy"
    )
    observable = next(
        node for node in primary["chain"]["nodes"] if node["stage"] == "observable_result"
    )
    observable["evidence_ids"] = ["GLOBAL_001"]

    with pytest.raises(ValueError, match="observable_result"):
        review_agent.validate_trace_against_snapshot(
            MarketTraceResult.model_validate(trace_dict), TRACE_SNAPSHOT
        )


def _patch_snapshot_and_llm(
    mocker,
    llm_content: str | Exception,
    snapshot: MarketTraceSnapshot | None = None,
) -> object:
    """统一 mock build_market_trace_snapshot + get_deep_think + archive，返回 mock_set_cache。"""
    snapshot_for_test = snapshot if snapshot is not None else TRACE_SNAPSHOT

    async def build_snapshot(_date: str):
        return snapshot_for_test

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
    llm.ainvoke.side_effect = lambda _messages: (
        order.append("llm") or AIMessage(content=VALID_TRACE_JSON)
    )
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)

    result = await review_agent.run(SCHEDULER_STATE)

    assert order == ["snapshot", "llm"]
    assert "## 归因结论" in result["final_response"]


# ============================================================================
# 有效 JSON — 渲染主因果链、备选解释和未解问题
# ============================================================================


@pytest.mark.asyncio
async def test_review_valid_trace_renders_primary_alternative_and_unresolved(mocker):
    """有效 JSON 返回主因果链、备选解释和未解问题。"""
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON)

    result = await review_agent.run(SCHEDULER_STATE)

    assert "## 归因结论" in result["final_response"]
    assert "## 候选解释与反证" in result["final_response"]
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
# 缓存命中 — 基于 trace+snapshot 重新渲染，不调用 LLM
# ============================================================================


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=_make_cached_artifact("cached review").model_dump(mode="json"),
)
async def test_review_run_cache_hit(_mock_cache):
    """缓存命中：基于 trace+snapshot 重新渲染，不调用 LLM。

    P1：缓存里的 markdown 文本不得直接返回；必须基于 artifact.trace +
    artifact.snapshot 重新调用 render_market_trace_markdown，以冻结 snapshot
    为事实来源重建展示层。
    """
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
    # 重新渲染的 Markdown 包含 snapshot 的主导现象事实，不包含缓存里的旧文本。
    assert "## 确认的市场现象" in result["final_response"]
    assert "cached review" not in result["final_response"]


@pytest.mark.asyncio
async def test_cache_without_frozen_discovery_rebuilds_snapshot(mocker):
    """缺失 discovery 的旧缓存不能命中，必须进入新快照构建路径。"""
    cached = _make_cached_artifact("legacy cached review").model_dump(mode="json")
    snapshot = cached["snapshot"]
    assert isinstance(snapshot, dict)
    snapshot.pop("phenomenon_discovery", None)
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=cached))
    rebuild = mocker.patch.object(
        review_agent, "build_market_trace_snapshot", new=AsyncMock(return_value=TRACE_SNAPSHOT)
    )
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))
    llm = AsyncMock()
    llm.ainvoke.return_value = AIMessage(content=VALID_TRACE_JSON)
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)

    await review_agent.run(SCHEDULER_STATE)

    rebuild.assert_awaited_once_with("2026-07-17")


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

    # P1：返回与持久化的都是重新渲染的 Markdown，不是缓存里的旧文本。
    assert "## 确认的市场现象" in result["final_response"]
    assert "cached scheduler review" not in result["final_response"]
    save.assert_awaited_once()
    kwargs = save.await_args.kwargs
    assert kwargs["report_type"] == "review"
    assert kwargs["report_date"] == "2026-07-17"
    assert kwargs["content"]["schema_version"] == "2.0"
    assert "## 确认的市场现象" in kwargs["content"]["display_report"]["details"]
    assert "cached scheduler review" not in kwargs["content"]["display_report"]["details"]


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

    # P1：返回的是重新渲染的 Markdown，不是缓存里的旧文本。
    assert "## 确认的市场现象" in result["final_response"]
    assert "cached manual review" not in result["final_response"]
    save.assert_not_awaited()


# ============================================================================
# scheduler 持久化失败不应影响复盘正常返回
# ============================================================================


@pytest.mark.asyncio
async def test_scheduler_persist_failure_keeps_review_response(mocker):
    """scheduler 持久化失败不应影响复盘正常返回（降级吞掉异常）。"""
    _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON)
    mocker.patch.object(review_agent, "archive_review")

    with patch.object(
        review_agent.node_api,
        "save_analysis_report",
        new=AsyncMock(side_effect=Exception("DB down")),
    ) as save:
        result = await review_agent.run(SCHEDULER_STATE)

    assert "## 归因结论" in result["final_response"]
    save.assert_awaited_once()


# ============================================================================
# P1 回归 — 缓存命中必须基于 trace+snapshot 重新渲染，不返回缓存里的展示层文本
# ============================================================================


@pytest.mark.asyncio
async def test_cache_hit_with_tampered_discovery_summary_is_rejected(mocker):
    """P1 回归：被篡改的冻结 discovery 不能作为缓存命中继续使用。"""
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    tampered_discovery = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={"primary": primary.model_copy(update={"summary": "TAMPERED_SUMMARY"})}
    )
    tampered_snapshot = TRACE_SNAPSHOT.model_copy(
        update={"phenomenon_discovery": tampered_discovery}
    )
    tampered_artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=tampered_snapshot,
        trace=MarketTraceResult.model_validate(VALID_TRACE_DICT),
        markdown="TAMPERED_CACHED_MARKDOWN",
        trace_summary="tampered cached summary",
        sectors=["tampered_sector"],
    )

    # 2. mock get_cached_review 返回被污染的工件
    mocker.patch.object(
        review_agent,
        "get_cached_review",
        new=AsyncMock(return_value=tampered_artifact.model_dump(mode="json")),
    )

    # 3. 缓存校验失败后会回退到新快照；此处让新快照失败以验证最终降级。
    fresh_snapshot = mocker.patch.object(
        review_agent,
        "build_market_trace_snapshot",
        new=AsyncMock(side_effect=RuntimeError("fresh snapshot unavailable")),
    )
    fresh_llm = mocker.patch.object(review_agent, "get_deep_think")

    # 4. 手动触发（非 scheduler），避免 save_analysis_report 网络调用
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

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    fresh_snapshot.assert_awaited_once_with("2026-07-17")
    fresh_llm.assert_not_called()


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
        trace["attribution_status"] = "insufficient"
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
async def test_review_rejects_legacy_trace_dominant_phenomenon(mocker):
    """1.1 trace 不再接受现象双写字段。"""

    def _add_legacy_phenomenon(trace: dict) -> None:
        trace["dominant_phenomenon"] = {
            "kind": "broad_rally",
            "summary": "旧双写",
            "fact_ids": ["INDEX_000001_SH"],
            "score": 3,
        }

    mock_set_cache = _patch_snapshot_and_llm(mocker, _trace_json_with(_add_legacy_phenomenon))
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_no_phenomenon_short_circuits_without_llm(mocker):
    """no_phenomenon 由服务端生成空归因，不调用 LLM。"""
    calm_a_share = copy.deepcopy(_A_SHARE)
    indexes = calm_a_share["indexes"]
    assert isinstance(indexes, dict)
    for index in indexes.values():
        assert isinstance(index, dict)
        index["pct_chg"] = 0.1
    calm_a_share["breadth"] = {
        "advance_ratio": 0.5,
        "total_count": 5000,
        "decline_count": 2400,
    }
    calm_a_share["turnover"] = {"change_pct": 1.0}
    calm_a_share["limits"] = {
        "up_count": 10,
        "down_count": 8,
        "broken_count": 1,
        "highest_board": 2,
    }
    no_phenomenon = discover_market_phenomenon(calm_a_share, _SOURCES, _CAPTURED_AT, [])
    assert no_phenomenon.status == "no_phenomenon"
    snapshot = TRACE_SNAPSHOT.model_copy(
        update={"a_share": calm_a_share, "phenomenon_discovery": no_phenomenon}
    )
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=None))
    mocker.patch.object(
        review_agent, "build_market_trace_snapshot", new=AsyncMock(return_value=snapshot)
    )
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    cache_set = AsyncMock(return_value=True)
    mocker.patch.object(review_agent, "set_cached_review", new=cache_set)
    llm_factory = mocker.patch.object(review_agent, "get_deep_think")

    result = await review_agent.run(SCHEDULER_STATE)

    assert "行情完整，未发现显著市场现象" in result["final_response"]
    cached_artifact = cache_set.await_args.args[1]
    assert cached_artifact["trace"]["unresolved_questions"] == ["未检测到明确的市场主导现象"]
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_source_map_mismatch_fails_before_archive_or_persistence(mocker) -> None:
    snapshot = _snapshot_with_mismatched_source_id()
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=None))
    mocker.patch.object(
        review_agent,
        "build_market_trace_snapshot",
        new=AsyncMock(return_value=snapshot),
    )
    archive_snapshot = mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    archive_review = mocker.patch.object(review_agent, "archive_review", return_value=True)
    cache_set = mocker.patch.object(
        review_agent,
        "set_cached_review",
        new=AsyncMock(return_value=True),
    )
    persist = mocker.patch.object(review_agent, "_persist_review_report", new=AsyncMock())
    llm_factory = mocker.patch.object(review_agent, "get_deep_think")

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    archive_snapshot.assert_not_called()
    archive_review.assert_not_called()
    cache_set.assert_not_awaited()
    persist.assert_not_awaited()
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_cached_source_map_mismatch_is_not_rendered_or_persisted(mocker) -> None:
    cached = _make_cached_artifact("poisoned cached review").model_copy(
        update={"snapshot": _snapshot_with_mismatched_source_id()}
    )
    mocker.patch.object(
        review_agent,
        "get_cached_review",
        new=AsyncMock(return_value=cached.model_dump(mode="json")),
    )
    rebuild = mocker.patch.object(
        review_agent,
        "build_market_trace_snapshot",
        new=AsyncMock(side_effect=RuntimeError("stop after rejecting cache")),
    )
    render = mocker.patch.object(
        review_agent,
        "render_market_trace_markdown",
        wraps=review_agent.render_market_trace_markdown,
    )
    persist = mocker.patch.object(review_agent, "_persist_review_report", new=AsyncMock())

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    rebuild.assert_awaited_once_with("2026-07-17")
    render.assert_not_called()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_insufficient_data_short_circuits_without_llm(mocker):
    """insufficient_data 由服务端生成空归因，不调用 LLM。"""
    missing_fields = ["a_share.indexes"]
    discovery = discover_market_phenomenon({}, {}, _CAPTURED_AT, missing_fields)
    snapshot = TRACE_SNAPSHOT.model_copy(
        update={
            "a_share": {},
            "sources": {},
            "missing_fields": missing_fields,
            "phenomenon_discovery": discovery,
        }
    )
    mocker.patch.object(review_agent, "get_cached_review", new=AsyncMock(return_value=None))
    mocker.patch.object(
        review_agent, "build_market_trace_snapshot", new=AsyncMock(return_value=snapshot)
    )
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    cache_set = AsyncMock(return_value=True)
    mocker.patch.object(review_agent, "set_cached_review", new=cache_set)
    llm_factory = mocker.patch.object(review_agent, "get_deep_think")

    result = await review_agent.run(SCHEDULER_STATE)

    assert "行情数据不足，无法可靠判断市场现象" in result["final_response"]
    cached_artifact = cache_set.await_args.args[1]
    assert cached_artifact["trace"]["unresolved_questions"] == [
        "市场数据不足以支撑归因分析",
        "因果证据充分性不足，依赖 partial 或 not_ready 来源",
        "快照缺少 1 个字段",
    ]
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_discovery_fact_id_not_in_sources(mocker):
    """discovery.fact_ids 必须引用真实 market_fact。"""
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    invalid = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={"primary": primary.model_copy(update={"fact_ids": ["NONEXISTENT_SOURCE"]})}
    )
    snapshot = TRACE_SNAPSHOT.model_copy(update={"phenomenon_discovery": invalid})
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON, snapshot=snapshot)
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
        trace["attribution_status"] = "hypothesis"
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


def test_confirmed_trace_requires_selected_supported_primary() -> None:
    trace_dict = copy.deepcopy(VALID_TRACE_DICT)
    for candidate in trace_dict["candidates"]:
        candidate["status"] = "insufficient"
        candidate["chain"] = None
    trace_dict["primary_chain_id"] = None
    trace_dict["alternative_chain_id"] = None

    with pytest.raises(ValueError, match="confirmed"):
        review_agent.validate_trace_against_snapshot(
            MarketTraceResult.model_validate(trace_dict), TRACE_SNAPSHOT
        )


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
async def test_review_degraded_when_discovery_fact_ids_swap_to_event_source(
    mocker,
):
    """discovery.fact_ids 不能引用事件证据。"""
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    invalid = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={"primary": primary.model_copy(update={"fact_ids": ["NEWS_001"]})}
    )
    snapshot = TRACE_SNAPSHOT.model_copy(update={"phenomenon_discovery": invalid})
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON, snapshot=snapshot)
    result = await review_agent.run(SCHEDULER_STATE)
    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_review_degraded_when_discovery_severity_is_tampered(mocker):
    """discovery 必须与确定性重算结果完全一致。"""
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    invalid = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={"primary": primary.model_copy(update={"severity": "low"})}
    )
    snapshot = TRACE_SNAPSHOT.model_copy(update={"phenomenon_discovery": invalid})
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON, snapshot=snapshot)
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
    stale_snapshot = TRACE_SNAPSHOT.model_copy(
        update={"snapshot_id": "trace-20260716", "trade_date": "2026-07-16"}
    )
    stale_artifact = ReviewArtifact(
        schema_version="1.1",
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
async def test_cache_hit_with_invalid_discovery_falls_back(mocker):
    """缓存中的 discovery 重算不一致时视为未命中。

    修复前：缓存命中只做 ReviewArtifact.model_validate，不再做跨对象校验；
    修复后：必须重新执行 validate_trace_against_snapshot。
    """
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    bad_discovery = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={"primary": primary.model_copy(update={"summary": "被污染的摘要"})}
    )
    bad_snapshot = TRACE_SNAPSHOT.model_copy(update={"phenomenon_discovery": bad_discovery})
    bad_artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=bad_snapshot,
        trace=MarketTraceResult.model_validate(VALID_TRACE_DICT),
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
    mock_set_cache = mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock())
    mock_save = mocker.patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock())

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
    mock_set_cache = mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock())
    mock_save = mocker.patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock())

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
    mock_save = mocker.patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock())

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


@pytest.mark.asyncio
async def test_review_degraded_when_discovery_fact_ids_are_duplicated(mocker):
    """新鲜 snapshot 的重复 discovery 事实 ID 必须被重算校验拒绝。"""
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    invalid = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={
            "primary": primary.model_copy(
                update={"fact_ids": ["INDEX_000001_SH", "INDEX_000001_SH"]}
            )
        }
    )
    snapshot = TRACE_SNAPSHOT.model_copy(update={"phenomenon_discovery": invalid})
    mock_set_cache = _patch_snapshot_and_llm(mocker, VALID_TRACE_JSON, snapshot=snapshot)

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    mock_set_cache.assert_not_called()


@pytest.mark.asyncio
async def test_cache_hit_with_duplicate_discovery_fact_ids_is_not_persisted(mocker):
    """缓存语义校验失败时，不得持久化该工件的 market_trace。"""
    primary = TRACE_SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    duplicated_discovery = TRACE_SNAPSHOT.phenomenon_discovery.model_copy(
        update={
            "primary": primary.model_copy(
                update={"fact_ids": ["INDEX_000001_SH", "INDEX_000001_SH"]}
            )
        }
    )
    duplicated_snapshot = TRACE_SNAPSHOT.model_copy(
        update={"phenomenon_discovery": duplicated_discovery}
    )
    cached_artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=duplicated_snapshot,
        trace=MarketTraceResult.model_validate(VALID_TRACE_DICT),
        markdown="invalid cached review",
        trace_summary="invalid cached summary",
        sectors=[],
    )
    mocker.patch.object(
        review_agent,
        "get_cached_review",
        new=AsyncMock(return_value=cached_artifact.model_dump(mode="json")),
    )
    fresh_snapshot = mocker.patch.object(
        review_agent,
        "build_market_trace_snapshot",
        new=AsyncMock(side_effect=RuntimeError("fresh snapshot unavailable")),
    )
    save = mocker.patch.object(
        review_agent.node_api,
        "save_analysis_report",
        new=AsyncMock(),
    )

    result = await review_agent.run(SCHEDULER_STATE)

    assert result["final_response"] == review_agent.DEGRADED_RESPONSE
    fresh_snapshot.assert_awaited_once_with("2026-07-17")
    save.assert_not_awaited()


# ============================================================================
# run_review 入口 — quick/full 双模式、覆盖检查、快照失败降级
# ============================================================================


@pytest.mark.asyncio
async def test_run_review_quick_success(mocker):
    """run_review(snapshot_kind=quick) 成功：调 build_quick_snapshot + 持久化。"""
    mocker.patch.object(
        review_agent.node_api, "get_analysis_report", new=AsyncMock(return_value=None)
    )
    mocker.patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock())
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.build_quick_snapshot",
        new=AsyncMock(return_value=TRACE_SNAPSHOT),
    )
    llm = AsyncMock()
    llm.ainvoke.return_value = AIMessage(content=VALID_TRACE_JSON)
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)

    result = await review_agent.run_review(
        report_date="2026-07-17",
        snapshot_kind="quick",
        trace_id="test-run-review-quick-001",
    )

    assert result.status == "ok"
    assert result.snapshot_kind == "quick"
    assert result.markdown


@pytest.mark.asyncio
async def test_run_review_quick_skipped_when_full_exists(mocker):
    """run_review(quick) 在已有 full 报告时返回 status=skipped。"""
    mocker.patch.object(
        review_agent.node_api,
        "get_analysis_report",
        new=AsyncMock(
            return_value={
                "report_type": "review",
                "data_source": "review_agent_full",
                "content": {},
            }
        ),
    )

    result = await review_agent.run_review(
        report_date="2026-07-17",
        snapshot_kind="quick",
        trace_id="test-run-review-skipped-001",
    )

    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_run_review_full_success(mocker):
    """run_review(snapshot_kind=full) 成功：调 build_market_trace_snapshot + 持久化。"""
    mocker.patch.object(
        review_agent.node_api, "get_analysis_report", new=AsyncMock(return_value=None)
    )
    save = mocker.patch.object(
        review_agent.node_api, "save_analysis_report", new=AsyncMock()
    )
    mocker.patch.object(review_agent, "archive_market_trace_snapshot")
    mocker.patch.object(review_agent, "archive_review", return_value=True)
    mocker.patch.object(review_agent, "set_cached_review", new=AsyncMock(return_value=True))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.build_market_trace_snapshot",
        new=AsyncMock(return_value=TRACE_SNAPSHOT),
    )
    llm = AsyncMock()
    llm.ainvoke.return_value = AIMessage(content=VALID_TRACE_JSON)
    mocker.patch.object(review_agent, "get_deep_think", return_value=llm)

    result = await review_agent.run_review(
        report_date="2026-07-17",
        snapshot_kind="full",
        trace_id="test-run-review-full-001",
    )

    assert result.status == "ok"
    assert result.snapshot_kind == "full"
    save.assert_awaited_once()
    call_kwargs = save.await_args.kwargs
    assert call_kwargs["data_source"] == "review_agent_full"


@pytest.mark.asyncio
async def test_run_review_degraded_on_snapshot_failure(mocker):
    """快照构建失败时返回 degraded。"""
    mocker.patch.object(
        review_agent.node_api, "get_analysis_report", new=AsyncMock(return_value=None)
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.build_quick_snapshot",
        new=AsyncMock(side_effect=Exception("snapshot unavailable")),
    )

    result = await review_agent.run_review(
        report_date="2026-07-17",
        snapshot_kind="quick",
        trace_id="test-run-review-degraded-001",
    )

    assert result.status == "degraded"
    assert review_agent.DEGRADED_RESPONSE in result.markdown


# ============================================================================
# Task 11 端到端集成测试 — 含 morning_forecast 的完整归因流程
# 验证 Task 1-10 改进点：晨报预判注入 + prediction_validation 输出 + 财联社电报来源
# ============================================================================


def _make_morning_forecast() -> MorningForecast:
    """构建测试用 MorningForecast（模拟 morning_forecast_extractor 输出）。"""
    return MorningForecast(
        report_date="2026-07-17",
        summary="央行降准释放流动性，看好金融与半导体板块",
        major_events=[
            MorningEvent(
                title="央行宣布降准0.5个百分点",
                direction="bullish",
                affected_sectors=["银行", "金融"],
            ),
        ],
        sectors=[
            MorningSectorView(sector="半导体", direction="bullish"),
            MorningSectorView(sector="银行", direction="bullish"),
        ],
        risks=["降准对银行净息差的长期影响尚不明确"],
        source_report_id="morning-2026-07-17",
    )


def _trace_with_prediction_validation() -> str:
    """在 VALID_TRACE_DICT 上追加 prediction_validation，返回 JSON 字符串。

    morning_forecast 非空时，validate_trace_against_snapshot 要求 trace 必须含
    prediction_validation，且 status ∈ {hit, partial, miss} 时 sector_hits 不得为空。
    """
    trace = copy.deepcopy(VALID_TRACE_DICT)
    trace["prediction_validation"] = {
        "status": "partial",
        "sector_hits": [
            {
                "sector": "半导体",
                "morning_direction": "bullish",
                "actual_direction": "bullish",
                "result": "hit",
                "deviation_note": "",
            },
            {
                "sector": "银行",
                "morning_direction": "bullish",
                "actual_direction": "neutral",
                "result": "miss",
                "deviation_note": "银行板块表现平淡，降准利好已被市场提前反映",
            },
        ],
        "event_hits": [
            {
                "event_title": "央行宣布降准0.5个百分点",
                "morning_direction": "bullish",
                "actual_impact": "金融板块上涨但涨幅有限",
                "result": "unverifiable",
                "note": "降准兑现但强度不及预判",
            },
        ],
        "overall_note": "板块方向部分命中，事件影响符合预期但强度不及预判",
    }
    return json.dumps(trace, ensure_ascii=False)


@pytest.mark.asyncio
async def test_review_agent_end_to_end_with_morning_forecast(mocker):
    """端到端：含 morning_forecast 的完整归因流程。

    验证 Task 1-10 改进点：
    1. ReviewArtifact.trace.prediction_validation 非空
    2. render_market_trace_markdown 含"预判对照"章节
    3. validate_trace_against_snapshot 通过
    4. snapshot.morning_forecast 非空
    5. snapshot.sources 含 NEWS_* 来自财联社（cls provider）
    """
    # 1. 构造含 morning_forecast 的 snapshot（复用 TRACE_SNAPSHOT 冻结事实）
    snapshot_with_forecast = TRACE_SNAPSHOT.model_copy(
        update={"morning_forecast": _make_morning_forecast()}
    )

    # 2. 构造含 prediction_validation 的 trace JSON
    trace_json = _trace_with_prediction_validation()

    # 3. mock 整条 review_agent.run 流水线（沿用 _patch_snapshot_and_llm 模式）
    mock_set_cache = _patch_snapshot_and_llm(
        mocker, trace_json, snapshot=snapshot_with_forecast
    )

    # 4. 触发 review_agent.run
    result = await review_agent.run(SCHEDULER_STATE)

    # 5. 不降级
    assert result["final_response"] != review_agent.DEGRADED_RESPONSE

    # 6. 验证 render 含"预判对照"章节（Task 1-10 新增的展示层）
    assert "## 预判对照" in result["final_response"]
    assert "板块方向对照" in result["final_response"]
    assert "事件影响对照" in result["final_response"]
    assert "部分命中" in result["final_response"]

    # 7. 验证 mock_set_cache 被调用，捕获写入的 artifact
    mock_set_cache.assert_called_once()
    cached_payload = mock_set_cache.await_args.args[1]
    cached_artifact = ReviewArtifact.model_validate(cached_payload)

    # 8. 验证 ReviewArtifact.trace.prediction_validation 非空
    pv = cached_artifact.trace.prediction_validation
    assert pv is not None
    assert pv.status == "partial"
    assert len(pv.sector_hits) == 2
    assert len(pv.event_hits) == 1
    # sector_hits 包含 hit 和 miss 两种结果
    hit_results = {hit.result for hit in pv.sector_hits}
    assert hit_results == {"hit", "miss"}

    # 9. 验证 snapshot.morning_forecast 非空（晨报预判成功注入）
    assert cached_artifact.snapshot.morning_forecast is not None
    assert cached_artifact.snapshot.morning_forecast.report_date == "2026-07-17"
    assert len(cached_artifact.snapshot.morning_forecast.sectors) == 2
    assert len(cached_artifact.snapshot.morning_forecast.major_events) == 1

    # 10. 验证 sources 含 NEWS_* 来自财联社（cls provider）
    news_source_ids = [
        sid
        for sid, src in cached_artifact.snapshot.sources.items()
        if sid.startswith("NEWS_")
    ]
    assert news_source_ids, "snapshot.sources 必须含 NEWS_* 来源"
    for sid in news_source_ids:
        news_src = cached_artifact.snapshot.sources[sid]
        assert news_src.kind == "event_evidence"
        assert news_src.provider == "cls"

    # 11. 验证 validate_trace_against_snapshot 通过（重跑校验不应抛异常）
    review_agent.validate_trace_against_snapshot(
        cached_artifact.trace, cached_artifact.snapshot
    )
