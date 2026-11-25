"""裁决层：冲突检测 + 研判定稿。

direction/confidence 只在这里算一次，所有出口读同一个 Judgment，
因此"各出口 direction/confidence/thesis 一致"是结构保证，不靠事后比对。
"""

from datetime import date

from aistock_agent.graph.brain.state import BrainState
from aistock_agent.schemas.judgment import (
    Judgment,
    JudgmentClaim,
    JudgmentConflict,
    JudgmentDirection,
    JudgmentRender,
)
from aistock_agent.utils.date import add_trading_days

_CONFLICT_GAP = 0.1
_SHORT_HORIZON_TRADING_DAYS = 5
_VERIFY_TRADING_DAYS = 3
_MODULES = [
    "market_perception",
    "insight_research",
    "bull_researcher",
    "bear_researcher",
    "conflict_resolver",
]


def detect_conflicts(claims: list[JudgmentClaim]) -> list[dict[str, object]]:
    """多空置信差 < 0.1 视为"没有分出胜负"，必须降级为中性并留痕。"""
    bull = next((item for item in claims if item.produced_by == "bull_researcher"), None)
    bear = next((item for item in claims if item.produced_by == "bear_researcher"), None)
    if bull is None or bear is None:
        return []
    if abs(bull.confidence - bear.confidence) >= _CONFLICT_GAP:
        return []
    return [
        {
            "conflict_id": f"cft_{bull.claim_id}",
            "claim_ids": [bull.claim_id, bear.claim_id],
            "description": "多空置信差 < 0.1，方向降级为中性",
            "resolution": "downgraded",
        }
    ]


async def conflict_resolver(state: BrainState) -> dict[str, object]:
    conflicts = detect_conflicts(state.get("claims") or [])
    return {
        "conflicts": conflicts,
        "trace": [{"node": "conflict_resolver", "status": "ok", "conflict_count": len(conflicts)}],
    }


def _direction(bull: JudgmentClaim, bear: JudgmentClaim) -> JudgmentDirection:
    if abs(bull.confidence - bear.confidence) < _CONFLICT_GAP:
        return "neutral"
    return "bullish" if bull.confidence > bear.confidence else "bearish"


def _to_conflict_models(raw: list[dict[str, object]]) -> list[JudgmentConflict]:
    return [JudgmentConflict.model_validate(item) for item in raw]


async def judgment_writer(state: BrainState) -> dict[str, object]:
    claims = state.get("claims") or []
    evidence = state.get("evidence") or []
    bull = next((item for item in claims if item.produced_by == "bull_researcher"), None)
    bear = next((item for item in claims if item.produced_by == "bear_researcher"), None)

    if bull is None or bear is None or not evidence:
        return {
            "judgment": None,
            "trace": [
                {"node": "judgment_writer", "status": "degraded", "error": "insufficient_claims"}
            ],
        }

    as_of = state["as_of"]
    subject_code = state["subject_code"]
    due_date = add_trading_days(date.fromisoformat(as_of), _SHORT_HORIZON_TRADING_DAYS)
    counter = [item for item in evidence if item.evidence_id in set(bear.evidence_ids)]
    if not counter:
        counter = [evidence[-1]]

    judgment = Judgment(
        judgment_id=f"jdg_{as_of.replace('-', '')}_{subject_code}",
        subject=bull.subject,
        subject_code=subject_code,
        subject_type="market",
        horizon="short",
        as_of=as_of,
        due_date=due_date.isoformat(),
        direction=_direction(bull, bear),
        confidence=round(max(bull.confidence, bear.confidence), 2),
        thesis=bull.statement,
        evidence=evidence,
        counter_evidence=counter,
        conflicts=_to_conflict_models(state.get("conflicts") or []),
        invalidators=bull.invalidated_by,
        verify_at=add_trading_days(due_date, _VERIFY_TRADING_DAYS).isoformat(),
        modules=list(_MODULES),
        render=JudgmentRender(
            home=True, stock_page=False, event_page=False,
            broadcast=True, push=True, chat=True, pick_page=False,
        ),
    )
    return {
        "judgment": judgment,
        "trace": [
            {"node": "judgment_writer", "status": "ok", "judgment_id": judgment.judgment_id}
        ],
    }
