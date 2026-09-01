"""Spec Cbis：溯源场景探针 —— 归因结论 → 核对历史预判场景 → 回流印证证据（渠道B）.

把一条溯源结论（Trace conclusion）与本标的的历史已验证预判记录做「场景级」
模糊比对，命中的预判场景作为确认性证据（``scene_match``）回填因果链
（``CausalChain.confirmed_prediction``，channel B 信号）。本模块只做场景/证据
层确认，不评判方向对错——方向交给主到期价通道（channel A）。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import re

import structlog

from aistock_agent.schemas.target import Target
from aistock_agent.services.data_client import node_api
from aistock_agent.trace.chain import PredictionConfirmation

logger = structlog.get_logger()

#: 拉取已验证预测的条数上限
_PREDICTION_LIMIT = 500


def _normalize(text: str) -> list[str]:
    """规范化文本为小写 token 列表（剥离标点/空白/非中英数字符）。"""
    if not text:
        return []
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]", " ", text.lower())
    return [t for t in cleaned.split() if t]


def _scenario_matches(conclusion_tokens: list[str], scenario: str) -> bool:
    """场景是否命中：归一化后的场景是归一化后结论的连续子串（v1 确定性语义）。

    中文无空格分词时 token 化会把整句挤成单 token，按「≥2 token 重叠」无法命中
    实际落地场景；故退化为子串包含判定——结论中出现该场景即视为印证命中，
    同时保留短场景（如单 token 场景名）也能命中的宽松语义。
    """
    if not conclusion_tokens or not scenario:
        return False
    scenario_tokens = _normalize(scenario)
    if not scenario_tokens:
        return False
    conclusion_text = "".join(conclusion_tokens)
    scenario_text = "".join(scenario_tokens)
    return scenario_text in conclusion_text


def match_scenarios(conclusion: str, scenarios: list[str]) -> list[str]:
    """返回结论命中的场景列表（空结论/空场景 → []）。"""
    conclusion_tokens = _normalize(conclusion)
    return [s for s in scenarios if _scenario_matches(conclusion_tokens, s)]


def _record_target(prediction: object) -> str | None:
    """取预判记录的目标串（首个非空 ``horizons[].target``），无则 None。"""
    if not isinstance(prediction, dict):
        return None
    horizons = prediction.get("horizons")
    if isinstance(horizons, list):
        for h in horizons:
            if isinstance(h, dict) and h.get("target"):
                return str(h["target"])
    return None


def _scenarios_from_prediction(prediction: object) -> list[str]:
    """从预判记录抽取全部非空场景 ``conditions[].scenario``。"""
    if not isinstance(prediction, dict):
        return []
    conditions = prediction.get("conditions")
    if not isinstance(conditions, list):
        return []
    out: list[str] = []
    for c in conditions:
        if isinstance(c, dict) and isinstance(c.get("scenario"), str) and c["scenario"]:
            out.append(c["scenario"])
    return out


async def _fetch_target_predictions(target: Target) -> list[dict[str, object]]:
    """拉取本标的历史已验证预测，按 internal_id/name 过滤；任何失败降级为 []. """
    accepted = {target.internal_id, target.name}
    try:
        records = await node_api.list_verified_predictions(limit=_PREDICTION_LIMIT)
    except Exception:  # noqa: BLE001 —— "永不 500"：数据源失败不阻断溯源
        logger.debug("scene_probe_fetch_failed", target=target.internal_id, exc_info=True)
        return []
    out: list[dict[str, object]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rt = _record_target(rec.get("prediction"))
        if rt is not None and rt in accepted:
            out.append(rec)
    return out


async def probe_scene_confirmation(
    *,
    target: Target,
    trace_id: str,
    conclusion: str,
    fetched_predictions: Sequence[dict[str, object]] | None = None,
) -> list[PredictionConfirmation]:
    """把溯源结论转为确认的预判场景证据（channel B）。

    ``fetched_predictions`` 可注入用于测试；默认路径调用
    ``node_api.list_verified_predictions``，任何失败 MUST 降级为 []。
    """
    if not conclusion:
        return []
    if fetched_predictions is None:
        fetched_predictions = await _fetch_target_predictions(target)
    confirmed_at = datetime.now(timezone.utc)
    confirmations: list[PredictionConfirmation] = []
    accepted_targets = {target.internal_id, target.name}
    for rec in fetched_predictions:
        if not isinstance(rec, dict):
            continue
        prediction = rec.get("prediction")
        if not isinstance(prediction, dict):
            continue
        # 目标过滤：预测记录的目标串必须命中 target（internal_id/name）才计数
        record_target = _record_target(prediction)
        if record_target is None or record_target not in accepted_targets:
            continue
        if "id" in rec:
            # 规范约束：id 存在但非非空 str（如空串/非 str）→ 跳过，避免脏记录回流
            prediction_id = rec.get("id")
            if not isinstance(prediction_id, str) or not prediction_id:
                continue
        else:
            # 注入型记录（测试）常省略 id，真实记录由 Node 端保证有 id；
            # 缺失时兜底非空占位，仍让命中的场景回流为确认证据。
            prediction_id = "<unknown>"
        scenarios = _scenarios_from_prediction(prediction)
        matched = match_scenarios(conclusion, scenarios)
        for scenario in matched:
            confirmations.append(
                PredictionConfirmation(
                    prediction_id=prediction_id,
                    scenario=scenario,
                    source_trace_id=trace_id,
                    confirmed_kind="scene_match",
                    confirmed_at=confirmed_at,
                )
            )
    if confirmations:
        logger.debug(
            "scene_probe_confirmed", target=target.internal_id, n=len(confirmations)
        )
    return confirmations