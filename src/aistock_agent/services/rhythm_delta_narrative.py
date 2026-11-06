"""delta_narrative：LLM 只判「本次变化的主次」并写一句 <=50 字（spec §7）。

LLM 只做一件事：判定「本次变化里哪一项是主要矛盾」；不重复仓位结论、不自算新数字。
任何失败（调用异常 / 解析失败 / 五条校验不过）一律落 `fallback_text()`：永不空、不抛。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from aistock_agent.prompts.workers.rhythm_delta import build_delta_prompt
from aistock_agent.schemas.rhythm_master import RhythmDelta
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.rhythm_engine import POSITION_LADDER
from aistock_agent.services.user_facing_copy import find_engineering_terms

logger = logging.getLogger(__name__)

_VALID_KEYS = {"gate", "mainline", "event", "position", "none"}
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# spec §7.4 第 3 条：禁止的是「直接下交易指令」的表述。
# 只装祈使句里真正的动作/仓位指令；「加仓 / 减仓」**不入表** —— 它们是描述性说法
# （spec §7.5 自己的模板句就是「离加仓还差 11.62 点」），禁掉会把正确文案也判死。
_IMPERATIVE = ("买入", "卖出", "满仓", "清仓")
_DISCLAIMER = "不构成投资建议"
_NUM_TOLERANCE = 0.01


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


def _strip_disclaimer(text: str) -> str:
    """剥离「不构成投资建议」（前端统一渲染，不重复）。剥离后可能以标点收尾，一并清掉。"""
    out = text.replace(_DISCLAIMER, "")
    return out.strip().strip("，,。. ").strip()


def _key_present(delta: RhythmDelta, key: str) -> bool:
    """main_key 必须在 delta 里真实存在的位置出现（spec §7.4 第 1 条）。

    gate ← trend（趋势压制就是关键条件卡着）、plus gates 的 gap 变化。
    """
    gap_keys = {g.key for g in delta.gaps}
    block = [*delta.blocking.resolved, *delta.blocking.added, *delta.blocking.persisting]
    if key == "none":
        return True
    if key == "position":
        return (
            delta.position.changed
            or delta.position.from_index is not None
            or delta.position.to_index is not None
        )
    if key == "gate":
        return "gate" in gap_keys or "trend" in block
    if key == "mainline":
        return "mainline" in gap_keys or "mainline" in block or delta.mainline is not None
    return "event" in gap_keys or "event" in block


def _has_imperative(text: str) -> bool:
    """是否含指令式表述（直接下交易指令）。"""
    return any(w in text for w in _IMPERATIVE)


def _delta_numbers(delta: RhythmDelta) -> list[float]:
    """delta 里出现过的全部数值（用于校验 LLM 没编数字）。"""
    nums: list[float] = []

    def _add(v: object) -> None:
        if isinstance(v, bool) or not isinstance(v, int | float):
            return
        nums.append(abs(float(v)))

    for g in delta.gaps:
        _add(g.from_gap)
        _add(g.to_gap)
    _add(delta.position.from_index)
    _add(delta.position.to_index)
    if delta.mainline is not None:
        _add(delta.mainline.from_excess)
        _add(delta.mainline.to_excess)
    if delta.review is not None:
        _add(delta.review.index_pct_chg)
        _add(delta.review.up_ratio)
    return nums


def validate_narrative(delta: RhythmDelta, raw: dict[str, Any]) -> tuple[bool, str]:
    """五条校验（spec §7.4）。返回 (是否可用, 原因)。"""
    key = str(raw.get("main_key") or "").strip()
    text = _strip_disclaimer(str(raw.get("text") or ""))
    if key not in _VALID_KEYS:
        return False, f"main_key 非法：{key}"
    if not _key_present(delta, key):
        return False, f"main_key 在差异数据里不存在：{key}"
    if not text:
        return False, "text 为空"
    if len(text) > 50:
        return False, f"text 超长：{len(text)}"
    hits = find_engineering_terms(text)
    if hits:
        return False, f"含工程词 {hits}"
    if _has_imperative(text):
        return False, "含指令式表述"
    allowed = _delta_numbers(delta)
    for num in _NUM_RE.findall(text):
        value = float(num)
        if not any(abs(value - a) <= _NUM_TOLERANCE for a in allowed):
            return False, f"数字未在差异数据中出现：{num}"
    return True, ""


async def run(delta: RhythmDelta) -> tuple[str, bool]:
    """LLM 生成，返回 `(文案, 是否真失败)`。永不空、不抛。

    两类降级要分开（spec §9）：
    - 调用异常 / 输出解析不出 JSON → `degraded=True`：属于「LLM 层没工作」，调用方写 data_missing；
    - 输出合规但校验不过 → `degraded=False`：只是模型没按规矩说，记日志即可，不上屏留痕。
    """
    try:
        llm = get_deep_think()
        resp = await llm.ainvoke(
            [{"role": "user", "content": build_delta_prompt(delta.model_dump())}]
        )
        raw = _parse_json(str(getattr(resp, "content", "") or ""))
        if not raw:
            logger.warning("rhythm_delta.narrative_unparsable")
            return fallback_text(delta), True
        ok, reason = validate_narrative(delta, raw)
        if not ok:
            logger.info("rhythm_delta.narrative_rejected reason=%s", reason)
            return fallback_text(delta), False
        text = _strip_disclaimer(str(raw.get("text") or "")) or fallback_text(delta)
        return text, False
    except Exception:
        logger.warning("rhythm_delta.narrative_failed", exc_info=True)
        return fallback_text(delta), True


def _parse_json(text: str) -> dict[str, Any]:
    """从模型输出里抠第一个 JSON 对象；抠不出返回空 dict（交给校验判失败）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}
