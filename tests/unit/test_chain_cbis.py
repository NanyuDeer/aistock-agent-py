from datetime import datetime

import pytest
from pydantic import ValidationError

from aistock_agent.trace.chain import CausalChain, CausalNode, PredictionConfirmation


@pytest.mark.asyncio
async def test_causal_chain_has_confirmed_prediction_default_empty() -> None:
    chain = CausalChain(nodes=[CausalNode(stage="trigger", claim="x", evidence_ids=[])])
    assert chain.confirmed_prediction == []


@pytest.mark.asyncio
async def test_causal_chain_accepts_confirmation() -> None:
    conf = PredictionConfirmation(
        prediction_id="p1",
        scenario="降息预期",
        source_trace_id="t1",
        confirmed_kind="scene_match",
        confirmed_at=datetime(2026, 9, 1),
    )
    chain = CausalChain(nodes=[], confirmed_prediction=[conf])
    assert chain.confirmed_prediction[0].scenario == "降息预期"
    assert chain.model_dump()["confirmed_prediction"][0]["confirmed_kind"] == "scene_match"


@pytest.mark.asyncio
async def test_confirmation_rejects_bad_kind() -> None:
    with pytest.raises(ValidationError):
        PredictionConfirmation(
            prediction_id="p1",
            scenario="s",
            source_trace_id="t",
            confirmed_kind="nope",
            confirmed_at=datetime(2026, 9, 1),
        )