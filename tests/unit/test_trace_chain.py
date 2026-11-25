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
    with pytest.raises(TraceChainError, match="chain stages mismatch"):
        validate_chain_stages(chain.nodes)


def test_validate_accepts_custom_stage_subset():
    chain = _chain("structural_root", "trigger")
    validate_chain_stages(chain.nodes, stages=["structural_root", "trigger"])


def test_market_trace_reexports_shared_chain_types():
    from aistock_agent.schemas.market_trace import CausalChain, CausalNode

    node = CausalNode(stage="trigger", claim="触发", evidence_ids=["e1"])
    chain = CausalChain(nodes=[node])
    assert chain.nodes[0].stage == "trigger"


def test_stock_trace_reexports_shared_chain_stage():
    from typing import get_args

    from aistock_agent.schemas.stock_trace import ChainStage

    assert set(get_args(ChainStage)) == set(TRACE_CHAIN_STAGES)


def test_stock_trace_validator_uses_shared_stages():
    from aistock_agent.services.stock_trace_validator import STAGES

    assert tuple(STAGES) == TRACE_CHAIN_STAGES


def test_shared_types_are_same_objects():
    import aistock_agent.trace.chain as chain_mod
    from aistock_agent.schemas.market_trace import CausalChain, CausalNode
    from aistock_agent.schemas.stock_trace import ChainStage
    from aistock_agent.services.stock_trace_validator import STAGES

    assert ChainStage is chain_mod.ChainStage
    assert CausalChain is chain_mod.CausalChain
    assert CausalNode is chain_mod.CausalNode
    assert STAGES is chain_mod.TRACE_CHAIN_STAGES


def test_trace_chain_error_is_value_error_subclass():
    assert issubclass(TraceChainError, ValueError)
