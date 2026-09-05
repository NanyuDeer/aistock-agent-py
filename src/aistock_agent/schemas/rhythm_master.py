# src/aistock_agent/schemas/rhythm_master.py
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal["ice", "launch", "rally", "overheat", "ebb"]
Certainty = Literal["high", "medium", "low"]
Direction = Literal["bullish", "bearish", "neutral"]
PositionAction = Literal["add", "hold", "reduce"]

# F3 裁决（2026-09-05）：score 由 level 派生同源（score=LEVEL_IDX×20，0-100 语义），
# Stage→前端五档映射表（对齐 frontend RhythmCard.vue LEVEL_META：ice/低/常/活/亢）。
# 确定性映射，禁止在 LLM/提示词层做。None（证据不足/数据缺）→ None（前端灰格）。
STAGE_TO_LEVEL: dict[str | None, dict[str, Any] | None] = {
    "ice": {"level": "ice", "score": 0},
    "ebb": {"level": "low", "score": 20},
    "launch": {"level": "normal", "score": 40},
    "rally": {"level": "active", "score": 60},
    "overheat": {"level": "euphoria", "score": 80},
    None: None,
}


class PositionBand(BaseModel):
    text: str
    action: PositionAction
    direction: Direction


class EventAnchor(BaseModel):
    event_date: str
    title: str
    confirm_condition: str
    direction_hint: Direction | None = None
    confidence: str | None = None


class RhythmEvidence(BaseModel):
    stage: Stage | None = None
    stage_reason: str = ""
    certainty: Certainty | None = None
    certainty_reason: str = ""
    position: PositionBand | None = None
    event_anchors: list[EventAnchor] = Field(default_factory=list)
    data_missing: list[str] = Field(default_factory=list)


class MainlineRef(BaseModel):
    name: str
    stage: str
    source: str
    data_date: str
    direction: Direction
    confidence: str


class LaunchOutlook(BaseModel):
    anchor_date: str
    title: str
    if_confirmed_direction: Direction
    confidence: str


class RhythmSynthesis(BaseModel):
    mainline: list[MainlineRef] = Field(default_factory=list)
    launch_outlook: list[LaunchOutlook] = Field(default_factory=list)
    narrative: str = ""


class MasterRhythmCard(BaseModel):
    basis_date: str
    target_date: str
    refresh_slot: str
    evidence: RhythmEvidence
    synthesis: RhythmSynthesis | None = None
    synthesis_available: bool = False
