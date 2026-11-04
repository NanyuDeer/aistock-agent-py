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
    # 仓位档位序号（0-4，因果输出 spec §4.1）；None = 未经 derive_position_index 产出
    index: int | None = None


class LedgerItem(BaseModel):
    """仓位账本单行（因果输出 spec §4.1）：原生数值 + 判定 + 对档位的作用。

    status 语义：blocking=正在压低档位 / neutral=有数据但无影响 /
    supporting=在抬高档位 / missing=数据不可用（**不得**当作 neutral）。
    """

    key: Literal["trend", "mainline", "event", "breadth"]
    value_text: str
    judgement: str
    effect: str
    status: Literal["blocking", "neutral", "supporting", "missing"]


class CandidateOutlook(BaseModel):
    name: str
    excess: float


class WatchPoint(BaseModel):
    """变奏观察点（因果输出 spec §4.2）：当前未满足项 + 当前距离。"""

    key: Literal["mainline", "gate", "event"]
    condition: str
    current_text: str
    sufficient_alone: bool
    candidates: list[CandidateOutlook] | None = None


class PositionLedger(BaseModel):
    """仓位推导账本（因果输出 spec §4.1）：position_index 的唯一真相源。

    items 恒 4 项且顺序固定 trend / mainline / event / breadth；缺失项
    status=missing 显式标注，不省略键。
    """

    items: list[LedgerItem]
    base_index: int | None = None
    adjust: int = 0
    final_index: int | None = None
    # 仅指数硬闸门（close<ma20 且 ma5<ma10<ma20）；语义不含主线破位/数据不足
    gate_hit: bool = False
    # 主线板块破位；None = 状态未知（无主线输入）
    mainline_breakdown: bool | None = None
    # K 线不足 20 根（趋势不可判，H5 禁止 +1）
    insufficient: bool = False
    # 真实「禁止 +1」标志 = gate_hit or mainline_breakdown or insufficient
    gate_blocked: bool = False
    mainline_state: str | None = None
    watch_points: list[WatchPoint] = Field(default_factory=list)
    ledger_version: str = "1.0"


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
