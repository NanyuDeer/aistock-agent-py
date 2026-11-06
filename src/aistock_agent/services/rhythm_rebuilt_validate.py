from __future__ import annotations

from aistock_agent.schemas.rhythm_master import RhythmEvidence, RhythmSynthesis

_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_DIRECTION = {"bullish", "bearish", "neutral"}


def validate_synthesis(synthesis: RhythmSynthesis, evidence: RhythmEvidence) -> bool:
    """空壳拒绝（launch_outlook 为空 = 无有效产出）。"""
    if not synthesis.launch_outlook:
        return False
    for outlook in synthesis.launch_outlook:
        if outlook.confidence not in _VALID_CONFIDENCE:
            return False
        if outlook.if_confirmed_direction not in _VALID_DIRECTION:
            return False
    return True


def prune_invalid(synthesis: RhythmSynthesis) -> RhythmSynthesis:
    """按元素剔除非法项（P1-2：避免「一条非法→整段作废」）。"""
    outlook = [
        o for o in synthesis.launch_outlook
        if o.confidence in _VALID_CONFIDENCE
        and o.if_confirmed_direction in _VALID_DIRECTION
    ]
    return RhythmSynthesis(launch_outlook=outlook)
