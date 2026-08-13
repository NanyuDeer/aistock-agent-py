"""B1a 共享 6 阶段链核心单元测试。"""

import pytest

from aistock_agent.trace.chain import (
    TRACE_CHAIN_STAGES,
    CausalChain,
    CausalNode,
    TraceChainError,
    validate_chain_stages,
)


def _node(stage: str) -> CausalNode:
    return CausalNode(stage=stage, claim=stage, evidence_ids=[])


def _chain(*stages: str) -> CausalChain:
    return CausalChain(nodes=[_node(s) for s in stages])


def test_stages_are_ordered_six_phases():
    assert list(TRACE_CHAIN_STAGES) == [
        "structural_root",
        "trigger",
        "transmission",
        "exposure",
        "repricing",
        "observable_result",
    ]


def test_validate_accepts_exact_six_stages():
    validate_chain_stages(_chain(*TRACE_CHAIN_STAGES).nodes)


def test_validate_rejects_out_of_order():
    chain = _chain(
        "trigger",
        "structural_root",
        "transmission",
        "exposure",
        "repricing",
        "observable_result",
    )
    with pytest.raises(TraceChainError, match="chain stages mismatch"):
        validate_chain_stages(chain.nodes)


def test_validate_rejects_missing_stage():
    chain = _chain(*TRACE_CHAIN_STAGES[:5])
    with pytest.raises(TraceChainError):
        validate_chain_stages(chain.nodes)


def test_validate_accepts_custom_stage_subset():
    chain = _chain("structural_root", "trigger")
    validate_chain_stages(chain.nodes, stages=["structural_root", "trigger"])
