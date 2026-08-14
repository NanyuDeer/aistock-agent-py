"""自选股洞察归因：LLM 输出规则校验与规则兜底打分（PRD 第 9 节第三阶段）。

主因决策分三阶段：候选抽取（Task 8，确定性规则）→ LLM 受约束选择与概括（Task 9，
候选集内选择）→ 本模块规则校验与兜底（确定性）。本模块两个入口：

1. ``validate_attribution``：校验 LLM 输出——主因候选存在且非 suppressed、label 非空
   且 ≤ ``settings.insight_label_max_chars`` 字、evidence_quote 在标题或正文中可检索。
   ``unconfirmed`` 仅当候选集无有效候选（为空或全部 suppressed）时合法。
2. ``rule_fallback_select``：LLM 失败或校验不过时回退，按证据分层打分选主因
   （L1 正文 10.0 / L2 量化 6.0 / L3 标题 3.0，乘以强度），suppressed 候选 0 分；
   输出 ``InsightResultPayload`` 同构 dict（``validation_status="rule_fallback"``）。

二期新增（证据包路径）：
3. ``validate_attribution_from_evidence``：证据包路径专用校验，复用候选存在性/非
   suppressed/label 长度规则，替换证据锚定判定为 ``_validate_driver_anchored_in_evidence``。
4. ``confidence_cap_for_evidence`` / ``_apply_cap``：T1/T2 证据上限 medium，业绩远期上限 low。
"""

import re

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


# ── 二期：证据包路径置信度联动 ──────────────────────────────────────────────────


def confidence_cap_for_evidence(candidates: list[CandidateFactor]) -> str | None:
    """主因证据来自 T1/T2 时置信度上限 medium；业绩特例远期（strength<0.3）上限 low（PRD §8 联动）。

    依赖 ``extract_candidates_from_evidence`` 在 CandidateFactor 填充 ``time_bucket``。
    """
    for c in candidates:
        if c.time_bucket in ("T1", "T2"):
            return "medium"
        if c.time_bucket == "earnings" and c.strength < 0.3:
            return "low"
    return None


def _apply_cap(confidence: str, cap: str | None) -> str:
    """置信度封顶：high → cap（medium/low），其余不变。

    ``cap`` 为 None 时不封顶；置信度等级排序 low < medium < high。
    """
    if cap is None:
        return confidence
    rank = {"low": 0, "medium": 1, "high": 2}
    return confidence if rank.get(confidence, 0) <= rank[cap] else cap


# ── 二期：证据包路径校验器锚定 ──────────────────────────────────────────────────


def _validate_driver_anchored_in_evidence(
    d: DriverOutput,
    by_id: dict[str, CandidateFactor],
    evidence: list[dict[str, object]],
) -> bool:
    """证据包路径锚定：候选存在性/非 suppressed/label 合规 + 证据锚定。

    一期 ``_validate_driver`` 的 evidence_quote 锚定判定（逐字检索原文）不适合量化候选
    （source='quant'）—— 其 evidence_quote 来自结构化字段、不在聚合正文里，直接复用会
    误判"未锚定"。

    量化候选（source='quant'）：锚定规则 = quote 中的关键片段（板块名/数字/百分号）在
    对应 source_type 证据的 title/excerpt 中可检索；文本候选（source='body'）：沿用
    evidence_quote 在对应证据的 title/excerpt 中可检索。
    """
    cand = by_id.get(d.candidate_id)
    if cand is None or cand.suppressed:
        return False
    if not d.label or len(d.label) > settings.insight_label_max_chars:
        return False
    if d.category is not None and d.category != cand.category:
        return False
    # 按 candidate id 定位源证据条目（id 格式 e{idx+1}，idx 为证据包原始索引）
    # 防护非数字后缀：校验器契约是"只返回 bool"，id 异常时视为未锚定而非抛错
    sid = cand.id
    if not sid.startswith("e") or not sid[1:].isdigit():
        return False
    target = evidence[int(sid[1:]) - 1]
    if not isinstance(target, dict):
        return False
    title = str(target.get("title") or "")
    excerpt = str(target.get("excerpt") or "")
    if cand.source == "quant":
        # 量化候选：提取 quote 中的关键 token（数字/百分号/汉字片段），任一可检索即锚定
        quote = cand.evidence_quote
        tokens = [
            t
            for t in re.findall(
                r"\d+(?:\.\d+)?%?|[A-Za-z0-9\u4e00-\u9fa5]{2,}", quote
            )
            if t
        ]
        return any(tok in title or tok in excerpt for tok in tokens)
    # 非量化候选：evidence_quote 在对应证据的 title/excerpt 中可检索
    return cand.evidence_quote in title or cand.evidence_quote in excerpt


def validate_attribution_from_evidence(
    output: InsightAttributionOutput,
    candidates: list[CandidateFactor],
    evidence: list[dict[str, object]],
) -> bool:
    """证据包路径专用校验：替换 ``validate_attribution`` 的证据锚定判定。

    - unconfirmed 语义：仅当候选集无有效候选时合法（同 ``validate_attribution``）。
    - 主因/次因校验：复用候选存在性/非 suppressed/label 长度/分类一致性规则，
      证据锚定改用 ``_validate_driver_anchored_in_evidence``。
    - 一期 ``validate_attribution`` 保持不变（零回归）。
    """
    if output.attribution_status == "unconfirmed":
        valid = [c for c in candidates if not c.suppressed]
        return len(valid) == 0
    if output.primary_driver is None:
        return False
    by_id = {c.id: c for c in candidates}
    if not _validate_driver_anchored_in_evidence(
        output.primary_driver, by_id, evidence
    ):
        return False
    return all(
        _validate_driver_anchored_in_evidence(d, by_id, evidence)
        for d in output.secondary_drivers
    )
