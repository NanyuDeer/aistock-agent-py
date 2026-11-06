"""delta_narrative：LLM 只判「本次变化的主次」并写一句 <=50 字（spec §7）。

本文件当前只落**模板兜底**（永不空）：`run()` 直接返回 `fallback_text()`。
LLM 调用 + 五条校验在下一批接入，届时 `run()` 内部改为「先 LLM，校验不过落模板」。
"""
from __future__ import annotations

from aistock_agent.schemas.rhythm_master import RhythmDelta
from aistock_agent.services.rhythm_engine import POSITION_LADDER


def _pos_text(idx: int | None) -> str:
    """档位序号 → 仓位文案（禁止出现「{n} 档」这类标识符）。"""
    if idx is None or not (0 <= idx < len(POSITION_LADDER)):
        return "未知仓位"
    return POSITION_LADDER[idx]


def _gap_of(delta: RhythmDelta, key: str) -> float | None:
    for g in delta.gaps:
        if g.key == key:
            return g.to_gap
    return None


def fallback_text(delta: RhythmDelta) -> str:
    """按 `default_key` + 确定性模板生成一句用户可见文案，永不返回空串（spec §7.5）。"""
    key = delta.default_key
    to_text = _pos_text(delta.position.to_index)

    if key == "position" and delta.position.changed:
        return f"仓位从{_pos_text(delta.position.from_index)}调到{to_text}"

    if key == "gate":
        gap = _gap_of(delta, "gate")
        if gap is not None:
            return f"离加仓还差 {gap:.2f} 点，仓位仍是{to_text}"
        return f"关键条件还卡着，仓位仍是{to_text}"

    if key == "mainline":
        m = delta.mainline
        if m is not None and m.from_name and m.to_name and m.from_name != m.to_name:
            return (
                f"主线从「{m.from_name}」换成「{m.to_name}」，"
                f"超额收益 {(m.from_excess or 0.0):+.2f} → {(m.to_excess or 0.0):+.2f} 个百分点"
            )
        if m is not None and m.to_name:
            return f"主线还是「{m.to_name}」，超额收益 {(m.to_excess or 0.0):+.2f} 个百分点"
        return f"主线没有明确方向，仓位仍是{to_text}"

    if key == "event":
        gap = _gap_of(delta, "event")
        if gap is not None:
            return f"离重要事件还差 {gap:.0f} 个交易日，仓位仍是{to_text}"
        return f"离重要事件越来越近，仓位仍是{to_text}"

    return "和上一条比没有实质变化，仓位和卡住的原因都没动"


async def run(delta: RhythmDelta) -> str:
    """生成 delta 叙事（当前：模板兜底；后续：LLM + 五条校验，失败落模板）。"""
    return fallback_text(delta)
