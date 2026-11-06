"""变奏观察点 + 溯源文案（因果输出 spec §5 / §6）——确定性纯函数，无 IO。

**单一真相源**：判定（均线闸门 / 主线三态 / 事件档位）全部在
`rhythm_engine.derive_position_index` 完成，本模块只读 `PositionLedger` 已落库的
中间量做「反解 + 翻译」，再重算一遍均线或主线会立即把系统拉回双真相源
（职责重定义 spec §2.1 问题 1）。

`build_watch_points` 只产出**当前未满足**项；`render_causality` 只产出**正在压低
档位**的项。两者共用同一份 `blocking` 推导（spec §6.2），故不会出现「文案说破位
但账本说未破位」。
"""
from __future__ import annotations

import re
from typing import Any

from aistock_agent.schemas.rhythm_master import (
    CandidateOutlook,
    CausalityText,
    GapValue,
    LedgerItem,
    PositionLedger,
    WatchPoint,
)
from aistock_agent.services.mainline_engine import (
    EXCESS_WEAK,
    GAP_EXCESS,
    MIN_CANDIDATES,
)
from aistock_agent.services.rhythm_engine import EVENT_WATCH_D

# 新格式（2026-11 文案去工程化）："收盘 3689.91 点，20 日均线 3701.53 点"
_TREND_VALUE_RE = re.compile(r"收盘\s*(-?\d+(?:\.\d+)?)\s*点.*?20\s*日均线\s*(-?\d+(?:\.\d+)?)")
# 旧格式（历史卡，生产库仍是此形状）："3689.91 / ma20 3701.53" —— 不得删，旧卡继承要解析
_TREND_VALUE_LEGACY_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*/\s*ma20\s+(-?\d+(?:\.\d+)?)")


def _parse_trend_value(text: str) -> tuple[float, float] | None:
    """解析 trend 行原生数值，新旧两种格式都认（旧卡兼容）。"""
    for pattern in (_TREND_VALUE_RE, _TREND_VALUE_LEGACY_RE):
        hit = pattern.search(text or "")
        if hit:
            return float(hit.group(1)), float(hit.group(2))
    return None

# 排序权重（spec §6.3）：闸门是硬约束，先解；同组内固定顺序。
_WATCH_ORDER: dict[str, int] = {"gate": 0, "mainline": 1, "event": 2}
_LEDGER_ORDER: dict[str, int] = {"trend": 0, "mainline": 1, "event": 2, "breadth": 3}

# 事件观察点文案（无事件标题入参，用统一指代，避免在生产侧伪造事件名）
_EVENT_CONDITION = "3 天内有重要数据或事件，同时主线转强"
_MAINLINE_MISSING = "板块数据不足"


def _blocking_keys(ledger: PositionLedger) -> list[str]:
    """阻塞项闭集（spec §6.2，唯一依据是 `gate_blocked` 而非 `gate_hit`）。

    `gate_blocked` 同时覆盖指数破位 / 主线破位 / K 线不足三种成因；用 `gate_hit`
    会漏掉后两者，出现「明明是 0 档却说没有阻塞项」。
    """
    keys: list[str] = []
    if ledger.gate_blocked:
        keys.append("gate")
    if ledger.mainline_state == "none":
        keys.append("mainline")
    return keys


def _close_and_ma20(
    ledger: PositionLedger, closes: list[float]
) -> tuple[float | None, float | None]:
    """close / ma20：优先用原始 K 线（与 derive 同口径 20 日均），不足 20 根时
    回退解析 trend 账本行的 `value_text`（不自行改窗口、不猜）。"""
    if len(closes) >= 20:
        return closes[-1], sum(closes[-20:]) / 20
    for item in ledger.items:
        if item.key == "trend":
            parsed = _parse_trend_value(item.value_text)
            if parsed is not None:
                return parsed
    return None, None


def _gate_watch_point(
    ledger: PositionLedger,
    *,
    closes: list[float],
    mainline_facts: dict[str, Any] | None,
    alone: bool,
) -> WatchPoint | None:
    """gate 观察点：condition 按**实际成因**三分（spec §6.2 表格）。

    `insufficient`（K 线不足）无法给出可执行条件 → 不产出（不猜）。
    """
    if not ledger.gate_blocked:
        return None
    if ledger.gate_hit:
        close, ma20 = _close_and_ma20(ledger, closes)
        if ma20 is None:
            # 数据不足时退化到不带点位的条件（仍如实描述需满足的形态）
            condition = "收盘站上 20 日均线，且 5 日均线回到 10 日均线上方"
        else:
            condition = f"收盘站上 {ma20:.2f} 点，且 5 日均线回到 10 日均线上方"
        if close is None or ma20 is None:
            current_text = "收盘价不可用"
        else:
            current_text = f"收盘 {close:.2f} 点，还差 {ma20 - close:.2f} 点"
        gap = (
            GapValue(
                key="gate", current=round(close, 2), target=round(ma20, 2),
                gap=round(ma20 - close, 2), unit="点", direction="up",
            )
            if (close is not None and ma20 is not None and ma20 > close)
            else None
        )
        return WatchPoint(
            key="gate", condition=condition, current_text=current_text,
            sufficient_alone=alone, gap=gap,
        )
    if ledger.mainline_breakdown:
        name = (mainline_facts or {}).get("name")
        current_text = f"{name} 已跌破 20 日线" if name else "主线板块已跌破 20 日线"
        return WatchPoint(
            key="gate", condition="主线板块止跌回升，重回 20 日线上方",
            current_text=current_text, sufficient_alone=alone,
        )
    return None


def _excess_clause() -> str:
    """主线成立所需的超额子句（condition / 溯源文案共用）。

    `EXCESS_WEAK <= 0` 时「≥0.0pct」是空门槛（任何正超额都过），写出来等于没写，
    此时主线不成立的**真实原因只剩间距**——故省掉超额子句，只留间距约束。
    """
    if EXCESS_WEAK > 0:
        return f"近 5 天超额 {EXCESS_WEAK:.1f} 个百分点以上，且领先第二名 {GAP_EXCESS:.1f} 个百分点以上"
    return f"近 5 天超额领先第二名 {GAP_EXCESS:.1f} 个百分点以上"


def _mainline_current_text(top3: list[CandidateOutlook]) -> str:
    """主线观察点的当前距离：只指向**真实卡住的那一条约束**。

    分支顺序即成因优先级（`EXCESS_WEAK = 0` 时主线不成立只剩「间距不足」与
    「有效候选不足」两种真实成因）：超额不够 → 间距不够 → 候选数不够。
    任一支都不得产出负数「差」：差额只在确未达标时用「达标值 − 当前值」计算。
    """
    if not top3:
        return _MAINLINE_MISSING
    best = top3[0]
    if best.excess < EXCESS_WEAK:
        return f"最高 {best.excess:+.1f} 个百分点（还差 {EXCESS_WEAK - best.excess:.1f} 个百分点）"
    if len(top3) < 2:
        # 单候选无法算间距（judge 此时收窄为 strong），指向候选数而非间距
        return f"只有 {len(top3)} 个候选板块，至少要 {MIN_CANDIDATES} 个"
    gap = best.excess - top3[1].excess
    if gap >= GAP_EXCESS and len(top3) < MIN_CANDIDATES:
        # 间距已达标却仍未成立 → 真实成因是有效候选数不足（rank_candidates 只收到这些）
        return f"只有 {len(top3)} 个候选板块，至少要 {MIN_CANDIDATES} 个"
    return f"领先第二名 {gap:.2f} 个百分点（需要 {GAP_EXCESS:.1f} 个百分点以上）"


def _mainline_watch_point(
    ledger: PositionLedger,
    *,
    candidates: list[CandidateOutlook] | None,
    alone: bool,
) -> WatchPoint | None:
    """mainline 观察点：`state != "established"` 才产出（spec §6.1）。

    `unavailable` / 无数据 → 只写「板块数据不足」，不伪造 condition、不带候选。
    """
    state = ledger.mainline_state
    if state == "established":
        return None
    if state != "none":
        return WatchPoint(
            key="mainline", condition=_MAINLINE_MISSING,
            current_text=_MAINLINE_MISSING, sufficient_alone=alone, candidates=None,
        )
    top3 = list(candidates or [])[:3] or None
    gap_obj = None
    if top3 and len(top3) >= 2:
        target = top3[1].excess + GAP_EXCESS
        gap_obj = GapValue(
            key="mainline", current=round(top3[0].excess, 2), target=round(target, 2),
            gap=round(max(target - top3[0].excess, 0.0), 2), unit="pct", direction="up",
        )
    return WatchPoint(
        key="mainline", condition=f"某个板块{_excess_clause()}",
        current_text=_mainline_current_text(top3 or []),
        sufficient_alone=alone, candidates=top3, gap=gap_obj,
    )


def _event_watch_point(
    ledger: PositionLedger,
    *,
    mainline_facts: dict[str, Any] | None,
    event_d: int | None,
    alone: bool,
) -> WatchPoint | None:
    """event 观察点：d 不在 {1,2}，或 d 在窗口内但主线未转强（spec §6.1）。"""
    if event_d is None:
        return None
    strength = (mainline_facts or {}).get("strength")
    if event_d in (1, 2) and strength == "strong":
        return None
    return WatchPoint(
        key="event", condition=_EVENT_CONDITION,
        current_text=f"距事件还有 {event_d} 个交易日",
        sufficient_alone=alone,
        gap=GapValue(
            key="event", current=float(event_d), target=float(EVENT_WATCH_D),
            gap=float(max(event_d - EVENT_WATCH_D, 0)), unit="交易日", direction="down",
        ),
    )


def build_watch_points(
    ledger: PositionLedger,
    *,
    mainline_facts: dict[str, Any] | None,
    event_d: int | None,
    closes: list[float],
    candidates: list[CandidateOutlook] | None = None,
) -> list[WatchPoint]:
    """变奏反解（spec §6）：只产出当前未满足项，最多 3 条。

    `sufficient_alone`（spec §6.2）：两个阻塞项同时存在（gate + mainline none）时
    任一单独解除都不够升档 → 全为 False；仅 1 个阻塞项 → 该项 True；其余项按
    「单条满足能否真的 +1」判定（event 仅在 base+adjust>1 时可能 alone 生效）。
    """
    blocking = _blocking_keys(ledger)
    multi = len(blocking) >= 2
    sole = blocking[0] if len(blocking) == 1 else None
    base = ledger.base_index
    event_alone = (
        not multi and base is not None and base + ledger.adjust > 1
    )

    points = [
        p for p in (
            _gate_watch_point(
                ledger, closes=closes, mainline_facts=mainline_facts,
                alone=sole == "gate",
            ),
            _mainline_watch_point(ledger, candidates=candidates, alone=sole == "mainline"),
            _event_watch_point(
                ledger, mainline_facts=mainline_facts, event_d=event_d, alone=event_alone
            ),
        ) if p is not None
    ]
    # spec §6.3：sufficient_alone 优先，其次 gate > mainline > event
    points.sort(key=lambda p: (not p.sufficient_alone, _WATCH_ORDER[p.key]))
    return points[:3]


def _trend_line(item: LedgerItem) -> str:
    """trend 阻塞行：数值从 value_text 解析，解析不出就用原文（不猜点位）。"""
    parsed = _parse_trend_value(item.value_text)
    if parsed is None:
        return f"指数 {item.value_text}，且均线空头排列"
    close, ma20 = parsed
    return f"指数 {close:.2f} 点跌破 20 日均线 {ma20:.2f} 点，且均线空头排列"


def _mainline_line(item: LedgerItem, ledger: PositionLedger) -> str:
    if ledger.mainline_breakdown or item.judgement == "主线破位":
        return "主线板块破位"
    return f"没有板块{_excess_clause()}，主线未成立"


def _event_line(item: LedgerItem) -> str:
    # 旧卡仍是「封顶轻仓」，新文案为「仓位封顶轻仓」——两种都要认
    if item.judgement in ("封顶轻仓", "仓位封顶轻仓"):
        return "事件没有落地，仓位封顶在轻仓"
    if item.judgement == "事件要求减仓":
        return "事件结果偏空，仓位下调"
    return "事件没有落地，仓位封顶在轻仓"


def _blocking_line(item: LedgerItem, ledger: PositionLedger) -> str:
    if item.key == "trend":
        return _trend_line(item)
    if item.key == "mainline":
        return _mainline_line(item, ledger)
    if item.key == "event":
        return _event_line(item)
    return f"{item.judgement}（{item.value_text}）"


def _positive_lead(ledger: PositionLedger) -> str:
    """无阻塞时的正向推导（spec §5.1 第三种首句）。"""
    from aistock_agent.services.rhythm_engine import POSITION_LADDER

    trend = next((i for i in ledger.items if i.key == "trend"), None)
    mainline = next((i for i in ledger.items if i.key == "mainline"), None)
    trend_j = "数据不可用" if trend is None else trend.judgement
    if trend_j == "多头排列":
        trend_j = "多头"
    ml_j = "数据不可用" if mainline is None else mainline.judgement
    idx = ledger.final_index
    final_text = (
        POSITION_LADDER[idx]
        if idx is not None and 0 <= idx < len(POSITION_LADDER)
        else "未知仓位"
    )
    return f"均线{trend_j}，主线{ml_j}，最后仓位定在{final_text}"


def render_causality(ledger: PositionLedger) -> CausalityText:
    """溯源主区文案（spec §5.1）：按 trend > mainline > event 收集 blocking，最多 2 条。

    条数与 blocking 项一一对应，前端只拼接不推断（G19）。`lead` 是首句：0 条时为
    完整正向推导；1 条时为「目前只卡在这一条」，明细在 `lines[0]`；2 条时为固定提示语。
    前端按 `lead + lines` 顺序拼接即得主区文案。

    「最多 2 条」成立的原因：优先级 1、2 任一为真都会把 base 压到 0，此时事件项
    不可能单独 +1。
    """
    blocking = sorted(
        (i for i in ledger.items if i.status == "blocking"),
        key=lambda i: _LEDGER_ORDER.get(i.key, len(_LEDGER_ORDER)),
    )[:2]
    lines = [_blocking_line(item, ledger) for item in blocking]
    if len(blocking) >= 2:
        lead = "两个条件同时卡着，只解决一个也还不能加仓"
    elif len(blocking) == 1:
        lead = "目前只卡在这一条"
    else:
        lead = _positive_lead(ledger)
    return CausalityText(lead=lead, lines=lines)
