# src/aistock_agent/schemas/rhythm_master.py
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal["ice", "launch", "rally", "overheat", "ebb"]
Certainty = Literal["high", "medium", "low"]
Direction = Literal["bullish", "bearish", "neutral"]
PositionAction = Literal["add", "hold", "reduce"]

# 阶段判定路径（2026-11-03）：区分「有独立证据算出」与「沿用别处结论」，
# 避免把中性兜底的沿用当成强判定展示（问题 1）。
# - computed：本地 detect_stage 依当日证据判出方向
# - inherited_prev_day：本地证据中性 → 沿用前一交易日 after_close 卡的主阶段
# - inherited_basis：盘前/午间 → 沿用同日 after_close 收盘基准卡的主阶段
StageSource = Literal["computed", "inherited_prev_day", "inherited_basis"]

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
    # 判定路径（问题 1）：None = 无阶段；computed = 本地独立判定；其余为沿用。
    stage_source: StageSource | None = None
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
    # 确定性主线事实（judge_mainline 输出 + 胜出候选 nav，worker 内部接线用；
    # 不直接序列化进 content，供 _build_rhythm_card 派生仓位文案/技术佐证）
    mainline_facts: dict[str, Any] | None = None
    # 盘前/午间沿用收盘基准时，一并沿用基准卡的仓位文案（2026-11-03）：
    # 档位来自 after_close 卡，仓位却用前一交易日 K 线重算会造成同日三时点口径打架，
    # 故继承时把基准卡 position_band.text 一并带过来，保证档位与仓位同源。
    inherited_position_text: str | None = None
