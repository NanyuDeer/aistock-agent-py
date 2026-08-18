"""Stock Trace Validator 的单元测试。"""

from datetime import datetime
from aistock_agent.schemas.stock_trace import (
    TraceCandidate,
    StockTraceResult,
    StockSourceRecord,
)


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
    )

    # 不抛异常即通过验证
    assert result is not None


def test_validate_selected_chain_shape_missing_layer_raises():
    """StockTraceResult 缺失必需层（如 capital）时，_validate_selected_chain_shape 抛出 ValueError。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="sector", rank=2, status="supported",
                       verdict="行业景气", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="market", rank=3, status="supported",
                       verdict="大盘向好", supporting_evidence_ids=[], counter_evidence_ids=[]),
        # 故意缺失 capital 层
        TraceCandidate(candidate_id="c5", layer="technical", rank=4, status="weak",
                       verdict="放量突破", supporting_evidence_ids=[], counter_evidence_ids=[]),
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
        )
        assert False, "Expected ValueError for missing required layer"
    except ValueError as e:
        expected_msg = "candidates must cover company, sector, market, capital and technical layers"
        assert expected_msg in str(e)


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
