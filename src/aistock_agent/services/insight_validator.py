"""自选股洞察归因：LLM 输出规则校验与规则兜底打分（PRD 第 9 节第三阶段）。

主因决策分三阶段：候选抽取（Task 8，确定性规则）→ LLM 受约束选择与概括（Task 9，
候选集内选择）→ 本模块规则校验与兜底（确定性）。本模块两个入口：

1. ``validate_attribution``：校验 LLM 输出——主因候选存在且非 suppressed、label 非空
   且 ≤ ``settings.insight_label_max_chars`` 字、evidence_quote 在标题或正文中可检索。
   ``unconfirmed`` 仅当候选集无有效候选（为空或全部 suppressed）时合法。
2. ``rule_fallback_select``：LLM 失败或校验不过时回退，按证据分层打分选主因
   （L1 正文 10.0 / L2 量化 6.0 / L3 标题 3.0，乘以强度），suppressed 候选 0 分；
   输出 ``InsightResultPayload`` 同构 dict（``validation_status="rule_fallback"``）。
"""

from aistock_agent.config import settings
from aistock_agent.schemas.insight import DriverOutput, InsightAttributionOutput
from aistock_agent.services.insight_candidate import CandidateFactor

# 证据分层基础分（PRD 第 9 节排序规则 1-7）：L1 正文直接证据 > L2 量化 > L3 标题
_SOURCE_BASE_SCORE: dict[str, float] = {"body": 10.0, "quant": 6.0, "title": 3.0}


def validate_attribution(
    output: InsightAttributionOutput,
    candidates: list[CandidateFactor],
    title: str,
    content: str,
) -> bool:
    """LLM 输出必须通过：候选存在、主因非 suppressed、label 长度合规、证据锚定原文。"""
    if output.attribution_status == "unconfirmed":
        # unconfirmed 语义严格收窄：仅当候选集无有效候选（为空或全部 suppressed）时合法。
        # 有有效候选却判 unconfirmed 说明 LLM 漏选，校验失败回退规则结果（保障归因率）。
        valid = [c for c in candidates if not c.suppressed]
        return len(valid) == 0
    if output.primary_driver is None:
        return False
    by_id = {c.id: c for c in candidates}
    if not _validate_driver(
        output.primary_driver, by_id, title, content, is_primary=True
    ):
        return False
    return all(
        _validate_driver(d, by_id, title, content, is_primary=False)
        for d in output.secondary_drivers
    )


def _validate_driver(
    d: DriverOutput,
    by_id: dict[str, CandidateFactor],
    title: str,
    content: str,
    is_primary: bool,
) -> bool:
    """单条 driver 校验：候选存在、非 suppressed、label 合规、证据锚定原文。

    ``is_primary`` 为简报签名保留参数（预留主因/次因差异化校验，当前两者校验一致）。
    """
    cand = by_id.get(d.candidate_id)
    if cand is None or cand.suppressed:
        return False
    if not d.label or len(d.label) > settings.insight_label_max_chars:
        return False
    # 分类权威在候选：LLM 若回传 category，必须与所选候选一致，否则拒绝（防 LLM 注入分类）。
    if d.category is not None and d.category != cand.category:
        return False
    # 证据锚定原文：候选的 evidence_quote 必须能在标题或正文中找到。
    # （label 为主题概括，允许概括生成，不要求逐字出现在原文。）
    return cand.evidence_quote in content or cand.evidence_quote in title


def rule_fallback_select(
    candidates: list[CandidateFactor], content: str, title: str
) -> dict[str, object]:
    """LLM 失败/校验不过时：按证据分层打分选主因。返回 InsightResultPayload 同构 dict。"""

    def score(c: CandidateFactor) -> float:
        if c.suppressed:
            return 0.0
        # L1 正文直接证据 > L2 量化 > L3 标题（基础分 × 强度）
        return _SOURCE_BASE_SCORE[c.source] * c.strength

    ranked = sorted(
        (c for c in candidates if not c.suppressed), key=score, reverse=True
    )
    if not ranked:
        return {
            "attribution_status": "unconfirmed",
            "confidence": "unconfirmed",
            "primary_driver": {},
            "secondary_drivers": [],
            "display_report": {"summary": "主因待验证", "details": _unconfirmed_text()},
            "podcast_brief": "价格异动已确认，主因待验证。",
            "validation_status": "rule_fallback",
            "model_provider": "rule",
        }
    primary = ranked[0]
    secondary = ranked[1:3]
    confidence = (
        "high"
        if primary.source == "body" and primary.strength >= 0.7
        else ("medium" if primary.source in ("body", "quant") else "low")
    )
    return {
        "attribution_status": "confirmed",
        "confidence": confidence,
        "primary_driver": {
            "label": primary.label[: settings.insight_label_max_chars],
            "category": primary.category,
            "confidence": confidence,
            "evidence_quote": primary.evidence_quote,
            "source_ids": [""],  # Node 侧按 event_id 关联 source_id
        },
        "secondary_drivers": [
            {
                "label": c.label[: settings.insight_label_max_chars],
                "category": c.category,
                "confidence": "low",
                "evidence_quote": c.evidence_quote,
                "source_ids": [""],
            }
            for c in secondary
        ],
        "display_report": {
            "summary": f"主导因素：{primary.label}",
            "details": _format_details(primary, secondary),
        },
        "podcast_brief": f"{primary.label}为主要推动因素。",
        "validation_status": "rule_fallback",
        "model_provider": "rule",
    }


def _format_details(
    primary: CandidateFactor, secondary: list[CandidateFactor]
) -> str:
    """简洁的 display_report.details：主导因素 + 证据 + 次因 + 证据。

    简报 Step 2 未给出实现，此处按 PRD 第 9 节展示契约自行落地，保持可读。
    """
    parts = [
        f"主导因素：{primary.label}（{primary.category}）。证据：{primary.evidence_quote}"
    ]
    parts.extend(
        f"次因：{c.label}（{c.category}）。证据：{c.evidence_quote}" for c in secondary
    )
    return "".join(parts)


def _unconfirmed_text() -> str:
    """标准"主因待验证"文案（PRD 第 9 节，与洞察卡片推送文案一致）。"""
    return (
        "价格异动已确认，但在当前证据窗口内未找到可验证的主导因素；"
        "建议关注后续公告、行业联动及资金变化。"
    )
