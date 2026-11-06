"""跨卡变更归因（spec §6）：纯函数，无 IO、无 LLM。

节奏是一根链：每张卡只跟「它上一条已经发生的卡」比。
- after_close(D) → 上一交易日的收盘卡
- morning(D)     → 同日 after_close（昨收那张收盘卡）
- midday(D)      → 同日 morning；缺失则回落 after_close（slot_fallback=True）
"""
from __future__ import annotations

from typing import Any, Literal, cast

from aistock_agent.schemas.rhythm_master import (
    DeltaBlocking,
    DeltaGap,
    DeltaMainline,
    DeltaPosition,
    DeltaRef,
    LedgerKey,
    MasterRhythmCard,
    RhythmDelta,
)

_DEFAULT_KEY_ORDER: tuple[LedgerKey, ...] = ("trend", "mainline", "event")
# blocking key → default_key 取值（trend 归到 gate：两者都是「关键条件卡着」）
_BLOCKING_TO_KEY: dict[LedgerKey, Literal["gate", "mainline", "event"]] = {
    "trend": "gate",
    "mainline": "mainline",
    "event": "event",
}
# 观察点结构化距离的单位（按 key 固定，不从上游串里解析）
_GAP_UNIT: dict[Literal["gate", "mainline", "event"], str] = {
    "gate": "点",
    "mainline": "pct",
    "event": "交易日",
}
_GAP_KEY_ORDER: tuple[Literal["gate", "mainline", "event"], ...] = ("gate", "mainline", "event")


def resolve_prev(target_date: str, slot: str, basis_date: str) -> tuple[str, str, bool]:
    """返回 (查询日期, 查询 slot, 是否 slot 回落)。"""
    if slot == "after_close":
        # 收盘卡比上一交易日的收盘卡
        return basis_date, "after_close", False
    if slot == "morning":
        # 盘前比同日昨收那张收盘卡（当日 after_close）
        return target_date, "after_close", False
    if slot == "midday":
        # 午间比同日 morning；缺失时回落同日 after_close
        return target_date, "morning", True
    return target_date, slot, False


def _blocking_keys(ledger: dict[str, Any] | None) -> list[LedgerKey]:
    """账本里 status=blocking 且属于 trend/mainline/event 的 key（breadth 不参与集合差）。"""
    if not ledger:
        return []
    keys: list[LedgerKey] = []
    for item in ledger.get("items") or []:
        key = item.get("key")
        if item.get("status") != "blocking" or key not in _DEFAULT_KEY_ORDER:
            continue
        ledger_key = cast(LedgerKey, key)
        if ledger_key not in keys:
            keys.append(ledger_key)
    return keys


def _gaps(watch_points: list[dict[str, Any]] | None) -> dict[str, float]:
    """观察点结构化距离 {key: gap}；无 gap 的项跳过。"""
    out: dict[str, float] = {}
    for wp in watch_points or []:
        gap = wp.get("gap")
        if not isinstance(gap, dict):
            continue
        key = gap.get("key") or wp.get("key")
        value = gap.get("gap")
        if isinstance(key, str) and isinstance(value, int | float):
            out[key] = float(value)
    return out


def _pick_default_key(
    blocking: DeltaBlocking, gaps: list[DeltaGap], position: DeltaPosition
) -> Literal["position", "gate", "mainline", "event", "none"]:
    """判定序（spec §6.3）：阻塞项变化 > gap 相对变化最大 > 仓位变化 > 无变化。

    相对变化 rel = |to-from| / max(|from|, 1.0)，分母下限 1.0 防 gap 近 0 时比值爆掉。
    """
    # 1) 阻塞项集合有变化（解除或新增）→ 按固定序取首个，trend 映射到 gate
    for key in _DEFAULT_KEY_ORDER:
        if key in blocking.resolved or key in blocking.added:
            return _BLOCKING_TO_KEY[key]

    # 2) gap 相对变化最大者；缺一侧按 0 计（约束从无到有/从有到无本身就是大变化）
    best: Literal["gate", "mainline", "event"] | None = None
    best_rel = 0.0
    for gap in gaps:
        from_gap = gap.from_gap if gap.from_gap is not None else 0.0
        to_gap = gap.to_gap if gap.to_gap is not None else 0.0
        rel = abs(to_gap - from_gap) / max(abs(from_gap), 1.0)
        if rel > best_rel:
            best_rel = rel
            best = gap.key
    if best is not None:
        return best

    # 3) 仓位档位变化
    if position.changed:
        return "position"

    return "none"


def build_delta(
    *,
    prev_content: dict[str, Any] | None,
    card: MasterRhythmCard,
    mainline_facts: dict[str, Any] | None,
    lag_days: int = 0,
    slot_fallback: bool = False,
) -> RhythmDelta | None:
    """构造 RhythmDelta；`prev_content` 为 None → 返回 None，由调用方留痕（spec §6.1）。"""
    if prev_content is None:
        return None

    prev_card: dict[str, Any] = prev_content.get("rhythm_card") or {}
    prev_ref = DeltaRef(
        target_date=str(prev_content.get("target_date") or ""),
        slot=str(prev_content.get("refresh_slot") or ""),
        created_at=prev_content.get("created_at"),
        lag_days=lag_days,
        slot_fallback=slot_fallback,
    )

    # 仓位：旧卡读顶层 position_index，缺失时回落 position_band.index
    from_index: int | None = prev_card.get("position_index")
    if from_index is None:
        from_index = (prev_card.get("position_band") or {}).get("index")
    to_index: int | None = (
        card.evidence.position.index if card.evidence.position is not None else None
    )
    position = DeltaPosition(
        from_index=from_index, to_index=to_index, changed=from_index != to_index
    )

    stage: dict[str, str | None] = {
        "from": prev_card.get("phase"),
        "to": card.evidence.stage,
    }

    # 旧卡（无 position_ledger）按 spec §9 降级：delta 只含 position/stage，
    # 其余维度置空——不得从当前卡侧倒推 gaps/added（那会把「没记过」说成「新增」）。
    prev_ledger: dict[str, Any] | None = prev_card.get("position_ledger") or None
    blocking = DeltaBlocking()
    gaps: list[DeltaGap] = []
    mainline: DeltaMainline | None = None

    if prev_ledger is not None:
        cur_ledger = card.evidence.position_ledger
        cur_ledger_dict: dict[str, Any] | None = (
            cur_ledger.model_dump() if cur_ledger is not None else None
        )

        # 两侧口径一致：都只取 trend/mainline/event，breadth 即使 blocking 也不参与
        prev_blocking = _blocking_keys(prev_ledger)
        cur_blocking = _blocking_keys(cur_ledger_dict)
        blocking = DeltaBlocking(
            resolved=[k for k in prev_blocking if k not in cur_blocking],
            added=[k for k in cur_blocking if k not in prev_blocking],
            persisting=[k for k in prev_blocking if k in cur_blocking],
        )

        prev_gaps = _gaps(prev_ledger.get("watch_points"))
        cur_gaps = _gaps(cur_ledger_dict.get("watch_points") if cur_ledger_dict else None)
        for gap_key in _GAP_KEY_ORDER:
            if gap_key not in prev_gaps and gap_key not in cur_gaps:
                continue
            gaps.append(
                DeltaGap(
                    key=gap_key,
                    from_gap=prev_gaps.get(gap_key),
                    to_gap=cur_gaps.get(gap_key),
                    unit=_GAP_UNIT[gap_key],
                )
            )

        snap: dict[str, Any] | None = prev_card.get("mainline_snapshot") or None
        if snap is not None:
            facts = mainline_facts
            mainline = DeltaMainline(
                from_name=snap.get("name"),
                from_excess=snap.get("excess"),
                to_name=facts.get("name") if facts else None,
                to_excess=facts.get("excess") if facts else None,
                strength_changed=(
                    facts is not None and snap.get("strength") != facts.get("strength")
                ),
            )

    return RhythmDelta(
        prev=prev_ref,
        position=position,
        stage=stage,
        blocking=blocking,
        gaps=gaps,
        mainline=mainline,
        default_key=_pick_default_key(blocking, gaps, position),
    )
