from datetime import datetime

import pytest

from aistock_agent.agents.workers.review import attach_confirmations_to_trace
from aistock_agent.schemas.market_trace import CandidateExplanation, MarketTraceResult
from aistock_agent.trace.chain import CausalChain, CausalNode, PredictionConfirmation


def _trace() -> MarketTraceResult:
    chain = CausalChain(nodes=[CausalNode(stage="trigger", claim="x", evidence_ids=[])])
    cand = CandidateExplanation(id="c1", category="domestic_macro_policy", status="supported",
                                verdict="v", chain=chain, supporting_evidence_ids=[], counter_evidence_ids=[])
    return MarketTraceResult(schema_version="1.1", attribution_status="confirmed",
                             candidates=[cand], primary_chain_id="c1", alternative_chain_id=None,
                             confidence="high", unresolved_questions=[], attribution_summary="s", prediction_validation=None)


def _conf() -> PredictionConfirmation:
    return PredictionConfirmation(prediction_id="p1", scenario="降息预期兑现", source_trace_id="tr1",
                                  confirmed_kind="scene_match", confirmed_at=datetime(2026, 9, 1))


def test_attach_writes_to_primary_chain() -> None:
    trace = _trace()
    assert attach_confirmations_to_trace(trace, [_conf()]) is True
    primary = next(c for c in trace.candidates if c.id == "c1")
    assert primary.chain is not None
    assert primary.chain.confirmed_prediction[0].scenario == "降息预期兑现"


def test_attach_skips_when_empty() -> None:
    trace = _trace()
    assert attach_confirmations_to_trace(trace, []) is False