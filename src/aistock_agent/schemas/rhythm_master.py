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
# - inherited_prev_day：本地证据中性 → 沿用前一交易日 after_close 卡的市场阶段
# - inherited_basis：盘前/午间 → 沿用同日 after_close 收盘卡的市场阶段
StageSource = Literal["computed", "inherited_prev_day", "inherited_basis"]

# F3 裁决（2026-09-05）：score 由 level 派生同源（score=LEVEL_IDX×20，0-100 语义），
# 确定性映射，禁止在 LLM/提示词层做。None（证据不足/数据缺）→ None（前端灰格）。
#
# DEPRECATED（2026-11-04，职责重定义 T4）：level / score 已不是产品概念。
#   - 节奏的核心产出是仓位档位（position_index 0-4），上色/短码一律走它
#   - 本表仅为未升级客户端兼容而保留产出，**不得删除、不得置 None**
#     （uni-app 三端，App 不升级即长期跑旧版，置 null 会让其日历变灰）
#   - 新代码禁止消费；前端已零引用，靠「零引用」完成语义废弃
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


class GapValue(BaseModel):
    """观察点距离的结构化数值（spec §5.1）：供跨日 delta 做差，避免解析中文串。

    `gap` 恒为正（>=0）：等于 0 表示该约束的数值部分已达标，卡点在别处
    （例如主线超额间距够了、但候选板块数不够）——与 `current_text` 的分工就在这里。
    """

    key: Literal["mainline", "gate", "event"]
    current: float
    target: float
    gap: float
    unit: Literal["点", "pct", "交易日"]
    # 需向哪个方向才满足：up = 当前值要涨到 target；down = 当前值要降到 target
    direction: Literal["up", "down"]


class WatchPoint(BaseModel):
    """变奏观察点（因果输出 spec §4.2）：当前未满足项 + 当前距离。"""

    key: Literal["mainline", "gate", "event"]
    condition: str
    current_text: str
    sufficient_alone: bool
    candidates: list[CandidateOutlook] | None = None
    # 结构化距离（spec §5.1）：单条件不可量化时为 None，前端继续用 current_text
    gap: GapValue | None = None


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
    # 事件调整**生效前**的档位 = clamp(base + 主线调整)，即 step2 结束、step3 之前的值
    # （职责重定义 spec §4.1）。盘前撤销「临近事件 +1」以它为基线：`沿用序号 − 1` 在
    # 4 档上界把 +1 吃掉时会多撤一档，只有事件前档位能无歧义还原。
    event_off_index: int | None = None
    # 事件调整的**已计入类型**（幂等闸门，职责重定义 spec §4.1/§4.2）：
    # - "approaching"：d∈{1,2} 的临近事件 +1 已计入（结果未知 → 盘前拿到结果后须先撤销）；
    # - "result"：已按事件结果定档（含结果「符合」导致 delta==0 的情形，不得重复计）；
    # - None：未计入事件调整（含 d==0 未落档的封顶——那是「结果未知」的安全约束）。
    event_applied_kind: Literal["approaching", "result"] | None = None
    watch_points: list[WatchPoint] = Field(default_factory=list)
    ledger_version: str = "1.0"


class CausalityText(BaseModel):
    """溯源主区文案（因果输出 spec §5.1）：首句 + 逐条阻塞明细。

    文案全部在 Python 侧确定性产出，前端只按序拼接，不拼字符串、不判阈值（G19）。
    """

    lead: str
    lines: list[str] = Field(default_factory=list)


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
    # 仓位推导账本（因果输出 spec §4.3）：与 position.index 同源同值，
    # position_index / 溯源 / 观察点全部从这一份账本派生；None = 仓位不可用（旧卡）。
    position_ledger: PositionLedger | None = None
    event_anchors: list[EventAnchor] = Field(default_factory=list)
    data_missing: list[str] = Field(default_factory=list)


class LaunchOutlook(BaseModel):
    anchor_date: str
    title: str
    if_confirmed_direction: Direction
    confidence: str


class RhythmSynthesis(BaseModel):
    """研研判层输出（2026-11 收缩）：仅保留事件情景预判。

    `narrative` 已删（复述确定性结论且会抄错仓位）；`mainline` 已删（抄写确定性事实）。
    `launch_outlook` 本批恒为空数组（缺 consensus 数据源），字段保留不破契约。
    """

    launch_outlook: list[LaunchOutlook] = Field(default_factory=list)


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
    # 盘前/午间沿用收盘卡时，一并沿用上游卡的仓位文案（2026-11-03）：
    # 档位来自 after_close 卡，仓位却用前一交易日 K 线重算会造成同日三时点口径打架，
    # 故继承时把上游卡 position_band.text 一并带过来，保证档位与仓位同源。
    inherited_position_text: str | None = None


LedgerKey = Literal["trend", "mainline", "event"]


class DeltaRef(BaseModel):
    """上一张卡的定位信息。"""

    target_date: str
    slot: str
    created_at: str | None = None
    # 两张卡的证据日相隔交易日数；>1 = 中间卡缺失
    lag_days: int = 0
    # 严格上一条缺失，已回落取更早的 slot
    slot_fallback: bool = False


class DeltaPosition(BaseModel):
    from_index: int | None = None
    to_index: int | None = None
    changed: bool = False


class DeltaBlocking(BaseModel):
    resolved: list[LedgerKey] = Field(default_factory=list)
    added: list[LedgerKey] = Field(default_factory=list)
    persisting: list[LedgerKey] = Field(default_factory=list)


class DeltaGap(BaseModel):
    key: Literal["mainline", "gate", "event"]
    from_gap: float | None = None
    to_gap: float | None = None
    unit: str = ""


class DeltaMainline(BaseModel):
    from_name: str | None = None
    to_name: str | None = None
    from_excess: float | None = None
    to_excess: float | None = None
    strength_changed: bool = False


class DeltaReview(BaseModel):
    """昨日判断 vs 昨日盘面，只陈述事实，不判对错（spec §6.2）。"""

    prev_target_date: str
    prev_position_text: str
    index_pct_chg: float
    up_ratio: float | None = None


class RhythmDelta(BaseModel):
    prev: DeltaRef
    position: DeltaPosition
    stage: dict[str, str | None] = Field(default_factory=dict)
    blocking: DeltaBlocking = Field(default_factory=DeltaBlocking)
    gaps: list[DeltaGap] = Field(default_factory=list)
    mainline: DeltaMainline | None = None
    review: DeltaReview | None = None
    default_key: Literal["position", "gate", "mainline", "event", "none"] = "none"
    version: str = "1.0"
