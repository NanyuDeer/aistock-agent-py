"""Stock Trace Validator 的单元测试。"""

from aistock_agent.schemas.stock_trace import TraceCandidate
from aistock_agent.services.stock_trace_validator import validate_stock_trace_result


def test_capital_candidate_layer_allowed():
    """capital layer 应可实例化（五层枚举扩展后允许）。"""
    candidate = TraceCandidate(
        candidate_id="c1", layer="capital", rank=2, status="supported",
        verdict="主力净流入放大", supporting_evidence_ids=["e1"], counter_evidence_ids=[],
    )
    assert candidate.layer == "capital"  # 五层枚举可实例化


def test_five_layer_candidates_accepted_in_result():
    """五层候选（含 capital/technical）可出现在归因结果中；枚举未扩展时 TraceCandidate 实例化即抛 ValidationError。"""
    candidates = [
        TraceCandidate(candidate_id="c1", layer="company", rank=1, status="supported",
                       verdict="业绩预增", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c2", layer="capital", rank=2, status="weak",
                       verdict="资金温和流入", supporting_evidence_ids=[], counter_evidence_ids=[]),
        TraceCandidate(candidate_id="c3", layer="technical", rank=3, status="weak",
                       verdict="放量突破", supporting_evidence_ids=[], counter_evidence_ids=[]),
    ]
    assert {c.layer for c in candidates} == {"company", "capital", "technical"}