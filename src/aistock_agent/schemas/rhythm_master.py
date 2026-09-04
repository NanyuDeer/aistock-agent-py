# src/aistock_agent/schemas/rhythm_master.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["ice", "launch", "rally", "overheat", "ebb"]
Certainty = Literal["high", "medium", "low"]
Direction = Literal["bullish", "bearish", "neutral"]
PositionAction = Literal["add", "hold", "reduce"]


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
