"""归因相似度评估器 —— 对照标准答案给 agent 归因输出评分。

评分口径（设计文档 8.1）：
- 方向一致性 0.2：bullish/bearish/neutral 结构化对比
- 归因要素命中 0.5：LLM 判定标准答案 drivers/transmission 是否被覆盖（语义相似）
- 行业/板块命中 0.3：affected_sectors 与 agent 输出板块的重叠率
"""

import json
from dataclasses import dataclass, field
from typing import cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.services import llm as llm_service

logger = structlog.get_logger()

_EXTRACT_PROMPT = """你是归因评估助手。从给定的大盘归因分析文本中提取结构化结论，
输出严格 JSON：{"direction": "bullish|bearish|neutral", "drivers": ["..."], "sectors": ["..."]}。
direction 取文本整体看多看空倾向；drivers 为驱动因素要点（≤5 条）；
sectors 为提到的行业/板块（≤8 个）。
只输出 JSON。"""

_DRIVER_JUDGE_PROMPT = """判断 agent 的归因 drivers 是否覆盖标准答案 drivers 的语义。
标准答案 drivers: {truth}
agent drivers: {agent}
对每条标准答案 drivers，若 agent 中能找到语义等价描述则命中。
输出严格 JSON：{{"hit_count": 整数, "total_count": 整数}}。只输出 JSON。"""


@dataclass
class ScoreDetail:
    direction: float
    drivers: float
    sectors: float
    total: float
    gap_analysis: str = field(default="")


async def extract_agent_attribution(text: str) -> dict[str, object]:
    """用 LLM 从 agent 输出文本提取结构化归因要点。"""
    llm = llm_service.get_deep_think()
    resp = await llm.ainvoke(
        [SystemMessage(content=_EXTRACT_PROMPT), HumanMessage(content=text[:4000])]
    )
    return _parse_json(str(resp.content))


async def evaluate_attribution(agent_output: str, ground_truth: dict[str, object]) -> ScoreDetail:
    """对 agent 单次归因输出评分（0-1）。"""
    attribution = ground_truth.get("attribution")
    if not isinstance(attribution, dict):
        return ScoreDetail(0.0, 0.0, 0.0, 0.0, gap_analysis="ground_truth 缺少 attribution")

    extracted = await extract_agent_attribution(agent_output)
    truth_dir = str(attribution.get("direction", "neutral"))
    agent_dir = str(extracted.get("direction", "neutral"))
    direction_score = (
        0.2 if _normalize_direction(truth_dir) == _normalize_direction(agent_dir) else 0.0
    )

    sectors_score = _sector_overlap_score(
        attribution.get("affected_sectors", []), extracted.get("sectors", [])
    )

    drivers_score = await _driver_hit_score(
        attribution.get("drivers", []), extracted.get("drivers", [])
    )

    total = round(direction_score + drivers_score + sectors_score, 4)
    gap_analysis = _build_gap_analysis(
        direction_score, drivers_score, sectors_score, attribution, extracted
    )
    return ScoreDetail(
        direction=direction_score,
        drivers=drivers_score,
        sectors=sectors_score,
        total=total,
        gap_analysis=gap_analysis,
    )


def _normalize_direction(value: str) -> str:
    if value not in {"bullish", "bearish", "neutral"}:
        return "neutral"
    return value


def _sector_overlap_score(truth_sectors: object, agent_sectors: object) -> float:
    truth = _as_str_list(truth_sectors)
    agent = _as_str_list(agent_sectors)
    if not truth:
        return 0.3  # 标准答案无板块要求则给满（无对比对象）
    if not agent:
        return 0.0
    hit = sum(1 for t in truth if any(t in a or a in t for a in agent))
    return round(0.3 * hit / len(truth), 4)


async def _driver_hit_score(truth_drivers: object, agent_drivers: object) -> float:
    truth = _as_str_list(truth_drivers)
    agent = _as_str_list(agent_drivers)
    if not truth:
        return 0.5
    if not agent:
        return 0.0
    llm = llm_service.get_deep_think()
    resp = await llm.ainvoke(
        [
            SystemMessage(content=_DRIVER_JUDGE_PROMPT.format(truth=truth, agent=agent)),
            HumanMessage(content="请评估"),
        ]
    )
    parsed = _parse_json(str(resp.content))
    try:
        hit = int(cast("str | float | int", parsed.get("hit_count", 0)))
        total = int(cast("str | float | int", parsed.get("total_count", len(truth))))
    except (TypeError, ValueError):
        hit, total = 0, len(truth)
    if total <= 0:
        return 0.0
    return round(0.5 * min(hit, total) / total, 4)


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _parse_json(text: str) -> dict[str, object]:
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    raw = match.group(0) if match else text
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("iterate_evaluator_llm_invalid_json", snippet=raw[:200])
        return {}


def _build_gap_analysis(
    direction_score: float,
    drivers_score: float,
    sectors_score: float,
    attribution: dict[str, object],
    extracted: dict[str, object],
) -> str:
    gaps: list[str] = []
    if direction_score == 0.0:
        gaps.append(f"方向不一致：标准答案={attribution.get('direction')}，agent={extracted.get('direction')}")
    if sectors_score < 0.15:
        gaps.append(
            f"板块覆盖不足：标准答案={attribution.get('affected_sectors')}，agent={extracted.get('sectors')}"
        )
    if drivers_score < 0.25:
        gaps.append(f"驱动要素覆盖不足：标准答案={attribution.get('drivers')}")
    return "；".join(gaps) if gaps else "无显著差距"
