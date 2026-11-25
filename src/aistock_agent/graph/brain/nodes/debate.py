"""多空两方（第一期确定性规则）。

关键设计：空方必须引用与多方**不同**的证据（回撤类证据），
否则 counter_evidence 只是多方证据的副本，等于没有反证。
"""

from aistock_agent.graph.brain.state import BrainState
from aistock_agent.schemas.judgment import JudgmentClaim

_BULL_CONFIDENCE = 0.62
_BEAR_CONFIDENCE = 0.42


async def bull_researcher(state: BrainState) -> dict[str, object]:
    claims = state.get("claims") or []
    if not claims:
        return {"trace": [{"node": "bull_researcher", "status": "degraded", "error": "no_claims"}]}

    base = claims[0]
    claim = JudgmentClaim(
        claim_id=f"clm_bull_{base.claim_id}",
        subject=base.subject,
        kind=base.kind,
        statement=f"维持判断：{base.statement}",
        evidence_ids=base.evidence_ids,
        confidence=_BULL_CONFIDENCE,
        invalidated_by=base.invalidated_by,
        produced_by="bull_researcher",
    )
    return {"claims": [claim], "trace": [{"node": "bull_researcher", "status": "ok"}]}


async def bear_researcher(state: BrainState) -> dict[str, object]:
    claims = state.get("claims") or []
    evidence = state.get("evidence") or []
    base = next((item for item in claims if item.produced_by == "insight_research"), None)
    risk = evidence[-1] if evidence else None

    if base is None or risk is None:
        return {
            "trace": [
                {"node": "bear_researcher", "status": "degraded", "error": "no_counter_evidence"}
            ]
        }

    claim = JudgmentClaim(
        claim_id=f"clm_bear_{base.claim_id}",
        subject=base.subject,
        kind=base.kind,
        statement=f"反向风险：{risk.claim}",
        evidence_ids=[risk.evidence_id],
        confidence=_BEAR_CONFIDENCE,
        invalidated_by=["指数重新站上近 5 日高点"],
        produced_by="bear_researcher",
    )
    return {"claims": [claim], "trace": [{"node": "bear_researcher", "status": "ok"}]}
