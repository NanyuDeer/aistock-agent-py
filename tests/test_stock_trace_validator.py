"""Stock Trace Validator 的单元测试。"""

from datetime import UTC, datetime

import pytest

from aistock_agent.prompts.workers.stock_trace import STOCK_TRACE_PROMPT
from aistock_agent.schemas.stock_trace import (
    SourceKind,
    StockSourceRecord,
    StockTraceResult,
    StockTraceSnapshot,
    TraceCandidate,
    TriggerEvent,
)
from aistock_agent.services.stock_trace_validator import (
    StockTraceValidationError,
    validate_stock_trace_result,
)

_WINDOW_END = datetime(2026, 7, 30, 2, 15, tzinfo=UTC)


def test_capital_candidate_layer_allowed():
    """capital layer 应可实例化（五层候选扩展后允许）。"""
    candidate = TraceCandidate(
        candidate_id="c1", layer="capital", rank=2, status="supported",
        verdict="主力资金入场放大", supporting_evidence_ids=["e1"], counter_evidence_ids=[],
    )
    assert candidate.layer == "capital"  # 五层候选可实例化


def test_five_layer_candidates_accepted_in_result():
    """三层候选（含 capital/technical）可出现在归因结果中。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="capital", rank=2, status="weak",
                       verdict="资金温和流入", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="technical", rank=3, status="weak",
                       verdict="放量突破", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]
    assert {c.layer for c in candidates} == {"company", "capital", "technical"}


def test_validate_selected_chain_shape_all_five_layers_passes():
    """StockTraceResult 含全部五层候选时，_validate_selected_chain_shape 验证通过。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="sector", rank=2, status="supported",
                       verdict="行业景气", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="market", rank=3, status="supported",
                       verdict="大盘向好", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c4", layer="capital", rank=4, status="weak",
                       verdict="资金流入", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c5", layer="technical", rank=5, status="weak",
                       verdict="放量突破", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]

    # 构造 StockTraceResult，chains 可为空列表
    result = StockTraceResult(
        schema_version="stock-trace-result-v1",
        event_id="event-001",
        snapshot_id="snapshot-001",
        analysis_version="v1",
        attribution_status="hypothesis",
        confidence_level="medium",
        confidence_score=0.6,
        candidates=candidates,
        chains=[],
        contradictions=[],
        unresolved_questions=[],
        suggested_actions=["observe"],
        primary_phrase="大盘向好",
    )

    # 不抛异常即通过验证
    assert result is not None


def test_validate_selected_chain_shape_missing_layer_raises():
    """StockTraceResult 缺失必产层（technical）时抛出 ValueError。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="sector", rank=2, status="supported",
                       verdict="行业景气", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="market", rank=3, status="supported",
                       verdict="大盘向好", supporting_evidence_ids=[], counter_evidence_ids=[]),
        # 故意缺失 technical 层（capital 已降级为条件准入层，缺失不再报错）
        TraceCandidate(candidate_id="c4", layer="capital", rank=4, status="weak",
                       verdict="资金流入", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]

    try:
        StockTraceResult(
            schema_version="stock-trace-result-v1",
            event_id="event-001",
            snapshot_id="snapshot-001",
            analysis_version="v1",
            attribution_status="hypothesis",
            confidence_level="low",
            confidence_score=0.5,
            candidates=candidates,
            chains=[],
            contradictions=[],
            unresolved_questions=[],
            suggested_actions=["observe"],
            primary_phrase="证据不足",
        )
        assert False, "Expected ValueError for missing required layer"
    except ValueError as e:
        expected_msg = "candidates must cover company, sector, market and technical layers"
        assert expected_msg in str(e)


def test_capital_layer_is_optional_in_result():
    """2026-09-18 决策：capital 降级为条件准入层——不产出 capital 候选不得阻塞结果校验。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="sector", rank=2, status="supported",
                       verdict="行业景气", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="market", rank=3, status="supported",
                       verdict="大盘向好", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c4", layer="technical", rank=4, status="weak",
                       verdict="放量突破", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]

    result = StockTraceResult(
        schema_version="stock-trace-result-v1",
        event_id="event-001",
        snapshot_id="snapshot-001",
        analysis_version="v1",
        attribution_status="hypothesis",
        confidence_level="medium",
        confidence_score=0.6,
        candidates=candidates,
        chains=[],
        contradictions=[],
        unresolved_questions=[],
        suggested_actions=["observe"],
        primary_phrase="大盘向好",
    )

    assert {c.layer for c in result.candidates} == {"company", "sector", "market", "technical"}


def test_prompt_downgrades_capital_to_conditional_layer():
    """2026-09-18 决策：capital 是传导/结果层，条件准入；禁止同义反复式归因。"""
    assert "传导" in STOCK_TRACE_PROMPT
    assert "同义反复" in STOCK_TRACE_PROMPT
    assert "结构、来源、背离" in STOCK_TRACE_PROMPT
    assert "不得作为 primary_chain 的支撑证据" in STOCK_TRACE_PROMPT
    # 仅有方向性数据时不得置 supported（防"资金方向与价格同向"式同义反复回潮）
    assert "capital 候选必须置" in STOCK_TRACE_PROMPT
    assert '不得因"资金方向与价格同向"而置 supported' in STOCK_TRACE_PROMPT
    # 时效分档：T-1 资金数据最高只能 weak（可作 alternative 驱动），不得支撑主链
    assert "最高只能置 weak" in STOCK_TRACE_PROMPT
    # 旧示例短语本身就是同义反复，必须移除
    assert "主力资金撤离" not in STOCK_TRACE_PROMPT


def test_prompt_requires_four_mandatory_layers_and_optional_capital():
    """四层必产候选、capital 为条件准入层；逐层评估但可对 capital 不产出条目。"""
    assert "必须逐一评估" in STOCK_TRACE_PROMPT
    assert "capital：资金面——条件准入层" in STOCK_TRACE_PROMPT


def test_prompt_requires_counter_evidence_on_opposite_sector_or_market_fact():
    """反向板块/大盘事实下仍置 supported 必须引用反证（与 Node 终态校验对齐）。"""
    assert "counter_evidence_ids 中引用该反向 source_id" in STOCK_TRACE_PROMPT
    assert "未引用时该层候选只能置 weak" in STOCK_TRACE_PROMPT


def _source(source_id: str, kind: SourceKind, payload: dict[str, object]) -> StockSourceRecord:
    return StockSourceRecord(
        source_id=source_id, kind=kind, provider="test", source_level="B",
        title=source_id, content_excerpt=source_id,
        occurred_at=_WINDOW_END, captured_at=_WINDOW_END,
        payload=payload, content_hash=source_id.ljust(64, "0")[:64],
    )


def _snapshot(*sources: StockSourceRecord) -> StockTraceSnapshot:
    return StockTraceSnapshot(
        snapshot_id="snapshot-001", event_id="event-001", trigger_revision=1,
        snapshot_stage="enriched", source_revision_hash="a" * 64,
        trigger_event=TriggerEvent(
            event_id="event-001", trigger_revision=1, symbol="000004", stock_name="Test",
            trading_date="2026-07-30", direction="up",
            triggered_at=_WINDOW_END, window_start_at=_WINDOW_END, window_end_at=_WINDOW_END,
            latest_price=22.0, previous_close=20.0, actual_value=10.0, threshold_value=7.0,
            severity="critical", rule_version="price-v1",
        ),
        missing_fields=[], data_readiness={}, collector_versions={},
        captured_at=_WINDOW_END, source_records=list(sources),
    )


def _result_with_sector(status: str, counter_ids: list[str]) -> StockTraceResult:
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="insufficient",
                       verdict="无公司证据", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="sector", rank=2, status=status,
                       verdict="板块联动", supporting_evidence_ids=[],
                       counter_evidence_ids=counter_ids),
        TraceCandidate(candidate_id="c3", layer="market", rank=3, status="insufficient",
                       verdict="无大盘证据", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c4", layer="technical", rank=4, status="insufficient",
                       verdict="无技术证据", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]
    return StockTraceResult(
        schema_version="stock-trace-result-v1", event_id="event-001",
        snapshot_id="snapshot-001", analysis_version="v1",
        attribution_status="hypothesis", confidence_level="low", confidence_score=0.3,
        candidates=candidates, chains=[], suggested_actions=["observe"],
        primary_phrase="证据不足",
    )


def test_supported_sector_candidate_requires_counter_evidence_on_opposite_fact():
    """板块反向下跌时仍称板块驱动（supported）却不引用反证 → 校验失败（给 LLM 纠错机会）。"""
    snapshot = _snapshot(
        _source("trigger-1", "trigger_fact", {}),
        _source("sector-down", "sector_fact", {"pct_change": -2}),
    )
    result = _result_with_sector("supported", [])
    with pytest.raises(StockTraceValidationError) as excinfo:
        validate_stock_trace_result(result, snapshot)
    assert "candidate:sector:missing_counter_evidence" in str(excinfo.value)


def test_supported_sector_candidate_passes_when_counter_evidence_cited():
    snapshot = _snapshot(
        _source("trigger-1", "trigger_fact", {}),
        _source("sector-down", "sector_fact", {"pct_change": -2}),
    )
    validate_stock_trace_result(_result_with_sector("supported", ["sector-down"]), snapshot)


def test_non_supported_sector_candidate_with_opposite_fact_not_blocked():
    """板块候选未声称驱动（weak）时不强制反证，避免误伤。"""
    snapshot = _snapshot(_source("sector-down", "sector_fact", {"pct_change": -2}))
    validate_stock_trace_result(_result_with_sector("weak", []), snapshot)


def test_source_kind_capital_fact_allowed():
    """StockSourceRecord 可使用 kind=capital_fact 实例化。"""
    record = StockSourceRecord(
        source_id="src-001",
        kind="capital_fact",
        provider="data_provider",
        source_level="B",
        title="北向资金增持",
        content_excerpt="北向资金今日增持100万股",
        captured_at=datetime.now(),
        content_hash="abc123",
    )
    assert record.kind == "capital_fact"


def test_source_kind_technical_fact_allowed():
    """StockSourceRecord 可使用 kind=technical_fact 实例化。"""
    record = StockSourceRecord(
        source_id="src-002",
        kind="technical_fact",
        provider="indicator",
        source_level="C",
        title="MACD金叉",
        content_excerpt="日线级别MACD金叉形成",
        captured_at=datetime.now(),
        content_hash="def456",
    )
    assert record.kind == "technical_fact"
