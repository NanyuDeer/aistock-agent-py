from __future__ import annotations

import re

from aistock_agent.schemas.rhythm_master import RhythmEvidence, RhythmSynthesis

_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_DIRECTION = {"bullish", "bearish", "neutral"}
_PRICE_POINT_RE = re.compile(r"[0-9]+(\.[0-9]+)?\s*(点|元|亿|%|％)")


def _contains_price_point(text: str) -> bool:
    return bool(_PRICE_POINT_RE.search(text or ""))


def _grounded(mainline_source: str, mainline_date: str) -> bool:
    return bool(mainline_source) and bool(mainline_date)


def validate_synthesis(synthesis: RhythmSynthesis, evidence: RhythmEvidence) -> bool:
    if _contains_price_point(synthesis.narrative):
        return False
    if not synthesis.mainline and not synthesis.launch_outlook and not synthesis.narrative.strip():
        # 三者皆空 = 空壳，拒绝（主线段空但 narrative/outlook 非空是合法态）
        return False
    for mainline in synthesis.mainline:
        if mainline.confidence not in _VALID_CONFIDENCE:
            return False
        if mainline.direction not in _VALID_DIRECTION:
            return False
        if not _grounded(mainline.source, mainline.data_date):
            return False
    for outlook in synthesis.launch_outlook:
        if outlook.confidence not in _VALID_CONFIDENCE:
            return False
        if outlook.if_confirmed_direction not in _VALID_DIRECTION:
            return False
    return True
