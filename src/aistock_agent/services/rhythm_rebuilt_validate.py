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


def _keep_mainline(
    m, candidate_names: set[str] | None, evidence_date: str | None
) -> bool:
    """单条 mainline 校验：基础规则 + 主线候选集/证据日约束（spec §7 / H2）。"""
    if m.confidence not in _VALID_CONFIDENCE:
        return False
    if m.direction not in _VALID_DIRECTION:
        return False
    if not _grounded(m.source, m.data_date):
        return False
    if candidate_names is not None and m.name not in candidate_names:
        return False
    if evidence_date is not None and m.data_date != evidence_date:
        return False
    return True


def validate_synthesis(
    synthesis: RhythmSynthesis, evidence: RhythmEvidence,
    *, candidate_names: set[str] | None = None, evidence_date: str | None = None,
) -> bool:
    if _contains_price_point(synthesis.narrative):
        return False
    if not synthesis.mainline and not synthesis.launch_outlook and not synthesis.narrative.strip():
        # 三者皆空 = 空壳，拒绝（主线段空但 narrative/outlook 非空是合法态）
        return False
    for mainline in synthesis.mainline:
        if not _keep_mainline(mainline, candidate_names, evidence_date):
            return False
    for outlook in synthesis.launch_outlook:
        if outlook.confidence not in _VALID_CONFIDENCE:
            return False
        if outlook.if_confirmed_direction not in _VALID_DIRECTION:
            return False
    return True


def prune_invalid(
    synthesis: RhythmSynthesis,
    *, candidate_names: set[str] | None = None, evidence_date: str | None = None,
) -> RhythmSynthesis:
    """按元素剔除非法项（P1-2：避免「一条非法→整段作废」）。

    复用 validate_synthesis 的子规则：mainline 需 confidence/direction 合法、
    source+data_date 非空，且在提供候选集/证据日时 name/data_date 精确匹配
    （spec §7 / H2：LLM 不得自创主线、不得用昨日数据冒充当日）。
    """
    mainline = [
        m for m in synthesis.mainline
        if _keep_mainline(m, candidate_names, evidence_date)
    ]
    outlook = [
        o for o in synthesis.launch_outlook
        if o.confidence in _VALID_CONFIDENCE
        and o.if_confirmed_direction in _VALID_DIRECTION
    ]
    return RhythmSynthesis(mainline=mainline, launch_outlook=outlook,
                           narrative=synthesis.narrative)
