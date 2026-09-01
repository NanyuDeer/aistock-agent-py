"""预判验证 skill（Spec B §4.1）—— 确定性判定之上的「画像读取 + 解释 + 反哺」复用层。

设计定位（不设独立 agent，预判 skill 同步调用）：
- 判定永远走确定性代码（prediction_validator._verify_horizon/_verify_conditions + run_once），
  本层只在判定之上做「画像读取 + LLM 解释」。
- 画像计算是纯函数（prediction_stats.build_validation_profile），本层管缓存与拉取编排。
- LLM 只做解释层（explain_verification，P3）；红线：只解释、不改判定、不产交易指令。

结构（对全局「四环三粒度」，验证环 §3.3）：
- read_validation_profile：缓存优先，miss 时拉 verified 重算（key 用 internal_id，§4.4）
- explain_verification / enrich_prediction_input：预判反哺入口（P3/P5 实现）

注意：profile 口径用 _PROFILE_METHODOLOGY_VERSION（=validator 现役 3.0），与
prediction_stats 默认的存量 2.0 口径分开——本层框住 run_once 当前写入的现役档。
"""

from __future__ import annotations

import json
from typing import cast

import structlog
from pydantic import BaseModel, Field

from aistock_agent.prompts.workers.prediction_validation import (
    PREDICTION_VALIDATION_PROMPT,
)
from aistock_agent.schemas.target import Target
from aistock_agent.services.cache import (
    get_cached_validation_profile,
    set_cached_validation_profile,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_quick_think, with_chat_structured_output
from aistock_agent.services.prediction_stats import build_validation_profile

logger = structlog.get_logger()

# 画像口径 = 验证器现役写入版本（prediction_validator._METHODOLOGY_VERSION=3.0）。
# 与 stats 默认 2.0（存量统计口径）刻意分开：画像要框住 run_once 当前写入的现役档（防混桶）。
_PROFILE_METHODOLOGY_VERSION = "3.0"

# 画像缓存 TTL（秒）：run_once 每日 16:00 更新，86400 即每日失效重算。
_PROFILE_CACHE_TTL = 86400


def _record_target(prediction: object) -> str | None:
    """取 prediction 的首个非空 target 字符串（画像分 target 重算用）。"""
    if not isinstance(prediction, dict):
        return None
    horizons = prediction.get("horizons")
    if isinstance(horizons, list):
        for h in horizons:
            if isinstance(h, dict) and h.get("target"):
                return str(h["target"])
    return None


def _slice_horizon(profile: dict[str, object], horizon: str) -> dict[str, object]:
    """把全量画像切成单一档位画像（读取时可选，不落缓存，避免 key 被单档污染）。"""
    hd = profile.get("horizon_breakdown")
    sub = hd.get(horizon) if isinstance(hd, dict) else None
    base = dict(profile)
    base["horizon"] = horizon
    if isinstance(sub, dict):
        base["n"] = sub.get("n", 0) or 0
        base["hits"] = sub.get("hits", 0) or 0
        base["hit_rate"] = sub.get("hit_rate", 0.0) or 0.0
        base["ci"] = sub.get("ci", [0.0, 0.0])
        base["sufficient_sample"] = bool(sub.get("sufficient_sample", False))
    else:
        base["n"] = 0
        base["hits"] = 0
        base["hit_rate"] = 0.0
        base["ci"] = [0.0, 0.0]
        base["sufficient_sample"] = False
    return base


async def _collect_target_entries(target: Target) -> list[dict[str, object]]:
    """从 verified 记录中收集该 target 的验证 entry（缓存 miss 时的重算数据源）。

    按 record 级 target 字符串匹配（== internal_id 或 == name），把该记录的
    verification 下所有带 result 的 entry 归入 target。数据源故障返回空列表（降级，
    build_validation_profile 得零画像，不 crash）。
    """
    entries: list[dict[str, object]] = []
    try:
        records = await node_api.list_verified_predictions(limit=500)
    except Exception:
        logger.debug("read_validation_profile_fetch_failed", target=target.internal_id,
                     exc_info=True)
        return entries
    accepted = {target.internal_id, target.name}
    for rec in records:
        rt = _record_target(rec.get("prediction"))
        if rt is None or rt not in accepted:
            continue
        ver = rec.get("verification")
        if not isinstance(ver, dict):
            continue
        for entry in ver.values():
            if isinstance(entry, dict) and "result" in entry:
                entries.append(entry)
    return entries


async def read_validation_profile(
    target: Target,
    horizon: str | None = None,
    ttl: int = _PROFILE_CACHE_TTL,
) -> dict[str, object]:
    """读取 target 的历史验证画像（缓存优先，miss 时拉 verified 重算）。

    Target 维度（全局 §2.1）：入参为全局 Target 对象，缓存 key 用 ``internal_id``
    （稳定标识，防板块改名断画像 + 码空间冲突）。

    Returns: {target, n, hits, hit_rate, ci, sufficient_sample, condition_met_rate,
              condition_summary, miss_patterns, horizon_breakdown, degradation_rate,
              source: "cache"|"rebuilt", cached: bool}（horizon 给定则叠加单档切片）。
    """
    cached = await get_cached_validation_profile(target.internal_id)
    if cached is not None:
        out = _slice_horizon(cached, horizon) if horizon else cached
        return {**out, "source": "cache", "cached": True}
    entries = await _collect_target_entries(target)
    profile = build_validation_profile(
        entries, target.internal_id, methodology_version=_PROFILE_METHODOLOGY_VERSION
    )
    await set_cached_validation_profile(target.internal_id, profile, ttl=ttl)
    out = _slice_horizon(profile, horizon) if horizon else profile
    return {**out, "source": "rebuilt", "cached": False}


class ValidationExplanation(BaseModel):
    """解释层结构化输出（Spec B §4.1）：把画像/条目解读成可读结论。"""

    summary: str = Field(description="一句话概括该 target 的历史验证表现")
    miss_reasons: list[str] = Field(description="失手原因归类，逐条概述")
    condition_met_insights: list[str] = Field(description="条件成立层面的规律洞察")
    prediction_implications: list[str] = Field(
        description="对后续预判输入的含义（仅供参考，不下判断、不产交易指令）"
    )


def _default_explanation(profile: dict[str, object]) -> dict[str, object]:
    """LLM 失效时的规则兜底解释（确定性，不依赖 LLM，仍满足红线：不改判定/不产交易指令）。"""
    n = int(cast(float, profile.get("n", 0)))
    rate = float(cast(float, profile.get("hit_rate", 0.0)))
    sufficient = bool(profile.get("sufficient_sample", False))
    cond_rate = profile.get("condition_met_rate")
    miss = profile.get("miss_patterns") or []
    miss_text = "；".join(
        f"{m.get('pattern')} x{m.get('count')}" for m in miss
    ) if isinstance(miss, list) else ""
    implications: list[str]
    if sufficient and rate < 0.5:
        implications = [f"该 target 命中率 {rate:.0%} 偏低且样本充足，预判时建议降档/补充条件"]
    elif not sufficient:
        implications = ["历史样本不足，暂不据此调整预判"]
    else:
        implications = ["命中率处于正常区间，历史验证对当前预判不做额外约束"]
    return {
        "summary": f"该 target 已验证 {n} 档，命中率 {rate:.0%}，样本{'充足' if sufficient else '不足'}",
        "miss_reasons": [miss_text] if miss_text else ["无失手样本可归类"],
        "condition_met_insights": (
            [f"条件化判定已确认 {cond_rate:.0%} 成立"] if isinstance(cond_rate, (int, float))
            else ["条件化判定样本尚不足"]
        ),
        "prediction_implications": implications,
    }


# 低命中率判定阈值：sufficient_sample（样本充足）且 hit_rate 低于该值 → 提示降置信/补条件
_LOW_HIT_RATE_THRESHOLD = 0.5


def enrich_prediction_input(
    base_input: dict[str, object], profile: dict[str, object]
) -> dict[str, object]:
    """把验证画像并入预判 LLM 输入（Spec B §4.3 反哺接口，纯函数不改原 dict）。

    新增 ``validation_profile`` 块（target/n/hit_rate/sufficient_sample/condition_met_rate），
    样本充足且命中率低时附 ``note``（"该 target 同类条件历史命中率低，请降低置信/补充条件"）。

    红线：只作**输入参考**——不改写 hit/miss 判定、不产交易指令、不在代码层钳制
    confidence（A3 置信钳制仍由产出方后处理覆盖，此处仅是 LLM 输入提示词上下文）。
    """
    n = int(cast(float, profile.get("n", 0)))
    rate = float(cast(float, profile.get("hit_rate", 0.0)))
    sufficient = bool(profile.get("sufficient_sample", False))
    ctx: dict[str, object] = {
        "target": profile.get("target"),
        "n": n,
        "hit_rate": rate,
        "sufficient_sample": sufficient,
        "condition_met_rate": profile.get("condition_met_rate"),
    }
    if sufficient and rate < _LOW_HIT_RATE_THRESHOLD:
        ctx["note"] = (
            f"该 target 同类条件历史命中率低（{rate:.0%}，n={n}），"
            "预判时刻意降低置信/补充更严条件；仅供输入参考，不产交易指令。"
        )
    out = dict(base_input)
    out["validation_profile"] = ctx
    return out


def _entries_digest(entries: list[dict[str, object]]) -> dict[str, object]:
    """把到期 entries 压缩成解释层可读的摘要（只取结构化字段，不塞原始 reason 长文本）。"""
    results: dict[str, int] = {}
    for e in entries:
        r = e.get("result")
        results[str(r)] = results.get(str(r), 0) + 1
    return {"result_counts": results, "entries": len(entries)}


async def explain_verification(
    profile: dict[str, object],
    entries: list[dict[str, object]],
) -> dict[str, object]:
    """LLM 解释层：把画像汇总成可读结论（Spec B §4.1）。

    LLM 只做解释，判定永远走确定性代码；LLM 失效时降级到规则兜底
    （_default_explanation），保证预判绑定不因解释失败中断。

    Returns: {summary, miss_reasons, condition_met_insights, prediction_implications}
    """
    inputs = {
        "profile_json": json.dumps(
            {
                "n": profile.get("n"),
                "hit_rate": profile.get("hit_rate"),
                "ci": profile.get("ci"),
                "sufficient_sample": profile.get("sufficient_sample"),
                "condition_met_rate": profile.get("condition_met_rate"),
                "condition_summary": profile.get("condition_summary"),
                "miss_patterns": profile.get("miss_patterns"),
            },
            ensure_ascii=False,
        ),
        "entries_summary": json.dumps(_entries_digest(entries), ensure_ascii=False),
    }
    try:
        llm = with_chat_structured_output(get_quick_think(), ValidationExplanation)
        out = await llm.ainvoke(inputs)
        return {
            "summary": out.summary,
            "miss_reasons": out.miss_reasons,
            "condition_met_insights": out.condition_met_insights,
            "prediction_implications": out.prediction_implications,
        }
    except Exception:  # noqa: BLE001
        logger.warning("explain_verification_llm_failed", target=profile.get("target"))
        return _default_explanation(profile)