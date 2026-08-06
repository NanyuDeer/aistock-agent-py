"""Review Agent — 收盘溯源归因（受限 JSON 推理）

模式：单次 get_deep_think().ainvoke，输入系统提示词 + JSON 快照
校验：MarketTraceResult.model_validate_json + validate_trace_against_snapshot
缓存：Redis TTL=2小时（briefing:review:YYYY-MM-DD）
归档：docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md

不再使用 ReAct 模式或工具调用。事实来自
build_market_trace_snapshot 冻结的 MarketTraceSnapshot，LLM 只做归因推理。
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.review import REVIEW_PROMPT
from aistock_agent.schemas.market_trace import (
    CandidateExplanation,
    DetectedPhenomenon,
    MarketTraceResult,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
    ReviewArtifact,
    SourceRecord,
)
from aistock_agent.services.archiver import (
    archive_market_trace_snapshot,
    archive_review,
)
from aistock_agent.services.cache import get_cached_review, set_cached_review
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.market_trace_snapshot import build_market_trace_snapshot
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()


@dataclass(frozen=True)
class ReviewRunResult:
    """run_review 的返回值。"""

    status: Literal["ok", "degraded", "skipped"]
    report_date: str
    snapshot_kind: Literal["quick", "full"]
    trace_id: str
    markdown: str

# ============================================================================
# 常量
# ============================================================================

# 4 个固定候选类别 — CandidateExplanation.id 必须与 category 同名
REQUIRED_CANDIDATE_CATEGORIES: set[str] = {
    "global_risk_liquidity",
    "domestic_macro_policy",
    "industry_technology_supply",
    "market_positioning_liquidity",
}

# primary/alternative chain 必须按顺序包含的 6 个阶段
REQUIRED_CHAIN_STAGES: list[str] = [
    "structural_root",
    "trigger",
    "transmission",
    "exposure",
    "repricing",
    "observable_result",
]

# 降级文本 — 校验失败、LLM 异常、快照构建失败时返回
DEGRADED_RESPONSE = "收盘溯源生成暂时不可用，请稍后重试"

# 代码围栏剥离 — 防御性处理 LLM 可能包裹的 ```json ... ```
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """剥离 LLM 可能包裹的 ```json ... ``` 代码围栏。"""
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


# ============================================================================
# LLM 输出字段名归一化 — prompt 强化的兜底
# ============================================================================

# LLM 常见错误字段名 → schema 正确字段名
_SECTOR_HIT_FIELD_MAP: dict[str, str] = {
    "predicted_direction": "morning_direction",
    "expected_direction": "morning_direction",
}

_EVENT_HIT_FIELD_MAP: dict[str, str] = {
    "event": "event_title",
    "title": "event_title",
    "predicted_direction": "morning_direction",
    "expected_direction": "morning_direction",
    "verification": "result",
    "actual_effect": "actual_impact",
    "impact": "actual_impact",
}

# 需要删除的多余字段
_FIELDS_TO_REMOVE: set[str] = {"evidence_ids"}

# 合法方向值
_DIRECTION_VALUES: set[str] = {"bullish", "bearish", "neutral"}
# SectorHit result 合法值
_SECTOR_RESULT_VALUES: set[str] = {"hit", "miss"}

# 相反方向映射（用于从 result=miss 推断 actual_direction）
_OPPOSITE_DIRECTION: dict[str, str] = {
    "bullish": "bearish",
    "bearish": "bullish",
    "neutral": "neutral",
}


def _normalize_sector_hit(hit: dict[str, object]) -> dict[str, object]:
    """归一化单个 SectorHit 字段名。"""
    nh = dict(hit)
    # 删除多余字段
    for f in _FIELDS_TO_REMOVE:
        nh.pop(f, None)
    # 字段名映射
    for wrong, correct in _SECTOR_HIT_FIELD_MAP.items():
        if wrong in nh:
            if correct not in nh:
                nh[correct] = nh[wrong]
            nh.pop(wrong)
    # 修正 actual_direction 被填入 result 值的情况（LLM 常犯错误）
    actual = nh.get("actual_direction")
    if actual and actual not in _DIRECTION_VALUES:
        # actual_direction 被填成了 hit/miss，移动到 result
        if actual in _SECTOR_RESULT_VALUES and "result" not in nh:
            nh["result"] = actual
        nh.pop("actual_direction", None)
    # 如果 actual_direction 缺失但有 result，从 morning_direction + result 推断
    if "result" in nh and "actual_direction" not in nh:
        morning_val = nh.get("morning_direction", "neutral")
        morning = morning_val if isinstance(morning_val, str) else "neutral"
        if nh["result"] == "hit":
            nh["actual_direction"] = morning
        elif nh["result"] == "miss":
            nh["actual_direction"] = _OPPOSITE_DIRECTION.get(morning, "neutral")
        else:
            nh["actual_direction"] = "neutral"
    # 确保 deviation_note 存在
    nh.setdefault("deviation_note", "")
    return nh


def _normalize_event_hit(evt: dict[str, object]) -> dict[str, object]:
    """归一化单个 EventHit 字段名。"""
    ne = dict(evt)
    # 删除多余字段
    for f in _FIELDS_TO_REMOVE:
        ne.pop(f, None)
    # 字段名映射
    for wrong, correct in _EVENT_HIT_FIELD_MAP.items():
        if wrong in ne:
            if correct not in ne:
                ne[correct] = ne[wrong]
            ne.pop(wrong)
    # 确保 actual_impact 存在
    ne.setdefault("actual_impact", "")
    # 确保 note 存在
    ne.setdefault("note", "")
    return ne


def _normalize_prediction_validation(raw_pv: dict[str, object]) -> dict[str, object]:
    """归一化 LLM 输出的 prediction_validation 字段名和值。

    LLM 经常用 predicted_direction/event/verification 等字段名，
    而非 schema 要求的 morning_direction/event_title/result。
    此函数在 model_validate 前做字段名映射，作为 prompt 强化的兜底。
    """
    pv = dict(raw_pv)

    # 归一化 sector_hits
    raw_hits = pv.get("sector_hits", [])
    if isinstance(raw_hits, list):
        pv["sector_hits"] = [
            _normalize_sector_hit(h) for h in raw_hits if isinstance(h, dict)
        ]

    # 归一化 event_hits
    raw_events = pv.get("event_hits", [])
    if isinstance(raw_events, list):
        pv["event_hits"] = [
            _normalize_event_hit(e) for e in raw_events if isinstance(e, dict)
        ]

    return pv


def _normalize_llm_trace_json(raw_json: str) -> str:
    """解析 LLM 输出的 JSON，归一化字段名与归因形态后返回。

    如果解析失败或无 prediction_validation，原样返回。
    除字段名归一化外，还兜底修正 hypothesis 归因的非法形态：
    - attribution_status="hypothesis" 时强制清空 primary_chain_id
      （hypothesis 表示证据不足、未确认主因，禁止选择主链）
    - hypothesis 时把 supported 候选降为 weak
      （hypothesis 只允许 weak 备选；LLM 可能在证据未闭环时误标 supported）
    否则 LLM 输出自相矛盾会被 validate_trace_against_snapshot 拒绝，
    导致整份报告降级为"生成暂时不可用"。
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return raw_json

    if not isinstance(data, dict):
        return raw_json

    pv = data.get("prediction_validation")
    if isinstance(pv, dict) and pv.get("status") != "no_forecast":
        data["prediction_validation"] = _normalize_prediction_validation(pv)

    if data.get("attribution_status") == "hypothesis":
        if data.get("primary_chain_id") is not None:
            data["primary_chain_id"] = None
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("status") == "supported":
                    candidate["status"] = "weak"

    return json.dumps(data, ensure_ascii=False)


# ============================================================================
# 跨对象校验 — 在 model_validate_json 之后做结构性检查
# ============================================================================


def validate_selected_chain_ids(trace: MarketTraceResult) -> None:
    """检查 primary_chain_id / alternative_chain_id 指向的候选存在且 status 符合要求。

    - 存在 supported 候选时，primary_chain_id 必须指向其中之一，不得为 null
    - 无 supported 候选时（全部 insufficient/rejected），primary_chain_id 必须为 null
    - alternative_chain_id 必须指向不同的 supported 或 weak 候选；null 允许
    - primary 与 alternative 不得相同

    修复：无论 primary 是否为 null，非空 alternative 都必须验证 ID 存在 + status
    合法（supported/weak）。旧实现 primary=null 时直接 return，跳过 alternative
    校验，导致 alternative 指向 rejected/insufficient 候选且链条完整时仍能通过。
    """
    candidate_by_id = {c.id: c for c in trace.candidates}
    has_supported = any(c.status == "supported" for c in trace.candidates)

    if trace.primary_chain_id is None:
        if has_supported:
            raise ValueError("primary_chain_id must not be null when supported candidates exist")
        # 无 supported 候选（全部 insufficient/rejected）：primary_chain_id=null 正确
        # 不再 return —— alternative 仍需校验（下方统一处理）
    else:
        primary = candidate_by_id.get(trace.primary_chain_id)
        if primary is None:
            raise ValueError(f"unknown primary_chain_id: {trace.primary_chain_id}")
        if primary.status != "supported":
            raise ValueError(
                f"primary_chain_id points to non-supported candidate: "
                f"{primary.id} ({primary.status})"
            )

    # 无论 primary 是否为 null，非空 alternative 都必须验证：
    # - ID 存在；status 只能是 supported 或 weak；不得与 primary 相同。
    if trace.alternative_chain_id is not None:
        if trace.alternative_chain_id == trace.primary_chain_id:
            raise ValueError("alternative_chain_id equals primary_chain_id")
        alternative = candidate_by_id.get(trace.alternative_chain_id)
        if alternative is None:
            raise ValueError(f"unknown alternative_chain_id: {trace.alternative_chain_id}")
        if alternative.status not in {"supported", "weak"}:
            raise ValueError(
                f"alternative_chain_id points to invalid status: "
                f"{alternative.id} ({alternative.status})"
            )


def validate_chain_stages(trace: MarketTraceResult) -> None:
    """primary 和非空 alternative 指向的 chain 必须恰好包含 6 个阶段（按顺序）。

    rejected/insufficient 候选可以没有 chain（chain=None）。
    无论 primary 是否为 null，非空 alternative 都必须通过 6 阶段校验：
    - primary_chain_id=None 时跳过 primary chain 校验；
    - alternative_chain_id 非空时仍校验 alternative chain 的 6 阶段顺序。
    """
    candidate_by_id = {c.id: c for c in trace.candidates}

    if trace.primary_chain_id is not None:
        primary = candidate_by_id.get(trace.primary_chain_id)
        if primary is None or primary.chain is None:
            raise ValueError("primary candidate has no chain")
        primary_stages = [n.stage for n in primary.chain.nodes]
        if primary_stages != REQUIRED_CHAIN_STAGES:
            raise ValueError(
                f"primary chain stages mismatch: {primary_stages} != {REQUIRED_CHAIN_STAGES}"
            )

    # 无论 primary 是否为 null，非空 alternative 都必须通过 6 阶段校验。
    # 修复前：primary_chain_id=None 时直接 return，跳过 alternative 校验，
    # 导致 primary=null + alternative 链条不完整的非法 trace 通过校验。
    if trace.alternative_chain_id is not None:
        alt = candidate_by_id.get(trace.alternative_chain_id)
        if alt is None or alt.chain is None:
            raise ValueError("alternative candidate has no chain")
        alt_stages = [n.stage for n in alt.chain.nodes]
        if alt_stages != REQUIRED_CHAIN_STAGES:
            raise ValueError(
                f"alternative chain stages mismatch: {alt_stages} != {REQUIRED_CHAIN_STAGES}"
            )


def _normalize_discovery_for_comparison(
    discovery: PhenomenonDiscoveryResult,
) -> tuple[object, ...]:
    """Canonicalize discovery references that JSONB may reorder."""

    def normalize_phenomenon(
        phenomenon: DetectedPhenomenon | None,
    ) -> tuple[object, ...] | None:
        if phenomenon is None:
            return None
        return (
            phenomenon.kind,
            phenomenon.summary,
            tuple(sorted(phenomenon.fact_ids)),
            tuple(phenomenon.tags),
            phenomenon.severity,
        )

    diagnostics: list[tuple[str, bool, tuple[str, ...]]] = []
    for diagnostic in discovery.diagnostics:
        evidence_ids = diagnostic.evidence_ids
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError(f"duplicate diagnostic evidence ID: {diagnostic.rule}")
        diagnostics.append((diagnostic.rule, diagnostic.matched, tuple(sorted(evidence_ids))))

    return (
        discovery.status,
        normalize_phenomenon(discovery.primary),
        tuple(normalize_phenomenon(item) for item in discovery.concurrent_phenomena),
        (
            discovery.data_readiness.market_data,
            discovery.data_readiness.attribution_inputs,
            discovery.data_readiness.causal_evidence,
        ),
        tuple(diagnostics),
    )


def validate_snapshot_discovery(snapshot: MarketTraceSnapshot) -> None:
    """重算冻结 discovery，并校验所有事实引用都是真实 market_fact。"""
    for source_id, record in snapshot.sources.items():
        if source_id != record.source_id:
            raise ValueError(f"source map key mismatch: {source_id} != {record.source_id}")
    recomputed = discover_market_phenomenon(
        snapshot.a_share,
        snapshot.sources,
        snapshot.captured_at,
        snapshot.missing_fields,
    )
    if _normalize_discovery_for_comparison(
        recomputed
    ) != _normalize_discovery_for_comparison(snapshot.phenomenon_discovery):
        raise ValueError("snapshot phenomenon discovery does not match recomputation")
    phenomena = []
    if snapshot.phenomenon_discovery.primary is not None:
        phenomena.append(snapshot.phenomenon_discovery.primary)
    phenomena.extend(snapshot.phenomenon_discovery.concurrent_phenomena)
    fact_ids = [fact_id for item in phenomena for fact_id in item.fact_ids]
    fact_ids.extend(
        fact_id
        for diagnostic in snapshot.phenomenon_discovery.diagnostics
        for fact_id in diagnostic.evidence_ids
    )
    for fact_id in fact_ids:
        source = snapshot.sources.get(fact_id)
        if source is None or source.kind != "market_fact":
            raise ValueError(f"discovery fact_id is not a market_fact: {fact_id}")


def _readiness_questions(
    discovery: PhenomenonDiscoveryResult,
    missing_fields: list[str],
) -> list[str]:
    """把确定性 discovery 的就绪状态转换为 QA 可见的未解问题。"""
    questions: list[str] = []
    if discovery.status == "no_phenomenon":
        questions.append("未检测到明确的市场主导现象")
    elif discovery.status == "insufficient_data":
        questions.append("市场数据不足以支撑归因分析")
    if discovery.data_readiness.causal_evidence != "ready":
        questions.append("因果证据充分性不足，依赖 partial 或 not_ready 来源")
    if missing_fields:
        questions.append(f"快照缺少 {len(missing_fields)} 个字段")
    return questions if questions else ["无需归因分析"]


def validate_trace_against_snapshot(
    trace: MarketTraceResult,
    snapshot: MarketTraceSnapshot,
) -> None:
    """校验 discovery、来源引用、归因状态、选链与六阶段链。"""
    validate_snapshot_discovery(snapshot)
    discovery = snapshot.phenomenon_discovery
    if discovery.status in {"no_phenomenon", "insufficient_data"}:
        expected = "not_applicable" if discovery.status == "no_phenomenon" else "insufficient"
        if (
            trace.attribution_status != expected
            or trace.candidates
            or trace.primary_chain_id is not None
            or trace.alternative_chain_id is not None
            or trace.confidence != "low"
            or trace.unresolved_questions
            != _readiness_questions(discovery, snapshot.missing_fields)
        ):
            raise ValueError("deterministic empty trace shape mismatch")
        return
    if trace.attribution_status == "not_applicable":
        raise ValueError("detected phenomenon cannot be not_applicable")

    categories = {candidate.category for candidate in trace.candidates}
    if (
        len(trace.candidates) != 4
        or categories != REQUIRED_CANDIDATE_CATEGORIES
        or {candidate.id for candidate in trace.candidates} != categories
    ):
        raise ValueError("candidate categories are incomplete")

    if trace.attribution_status == "confirmed":
        if discovery.data_readiness.causal_evidence != "ready":
            raise ValueError("confirmed attribution requires ready causal evidence")
        if trace.primary_chain_id is None:
            raise ValueError("confirmed attribution requires a supported primary chain")
    elif trace.attribution_status == "hypothesis":
        if trace.primary_chain_id is not None:
            raise ValueError("hypothesis must not select a primary chain")
        if any(candidate.status == "supported" for candidate in trace.candidates):
            raise ValueError("hypothesis cannot contain supported candidates")
        if trace.alternative_chain_id is not None:
            alternative = next(
                (c for c in trace.candidates if c.id == trace.alternative_chain_id), None
            )
            if alternative is None or alternative.status != "weak":
                raise ValueError("hypothesis alternative must be weak")
    elif trace.attribution_status == "insufficient":
        if trace.primary_chain_id is not None or trace.alternative_chain_id is not None:
            raise ValueError("insufficient attribution cannot select chains")
        if any(
            candidate.status not in {"insufficient", "rejected"} for candidate in trace.candidates
        ):
            raise ValueError("insufficient attribution has invalid candidate status")

    # 3. 每个候选的证据引用必须存在；每个非空 chain 节点必须有至少 1 个证据
    ids = set(snapshot.sources)
    for candidate in trace.candidates:
        for source_id in candidate.supporting_evidence_ids + candidate.counter_evidence_ids:
            if source_id not in ids:
                raise ValueError(f"unknown source_id: {source_id}")
        if candidate.chain is not None:
            for node in candidate.chain.nodes:
                if not node.evidence_ids:
                    raise ValueError(
                        f"candidate {candidate.id} node {node.stage} has empty evidence_ids"
                    )
                for source_id in node.evidence_ids:
                    if source_id not in ids:
                        raise ValueError(f"unknown source_id: {source_id}")

    candidate_by_id = {c.id: c for c in trace.candidates}
    chains_to_check: list[tuple[str, CandidateExplanation]] = []
    if trace.primary_chain_id is not None:
        primary = candidate_by_id.get(trace.primary_chain_id)
        if primary is not None and primary.chain is not None:
            chains_to_check.append(("primary", primary))
    if trace.alternative_chain_id is not None:
        alt = candidate_by_id.get(trace.alternative_chain_id)
        if alt is not None and alt.chain is not None:
            chains_to_check.append(("alternative", alt))
    validate_selected_chain_ids(trace)
    validate_chain_stages(trace)

    primary_phenomenon = discovery.primary
    if primary_phenomenon is None:
        raise ValueError("detected discovery has no primary phenomenon")
    primary_fact_ids = set(primary_phenomenon.fact_ids)
    for label, candidate in chains_to_check:
        chain = candidate.chain
        if chain is None:
            continue
        trigger = next(node for node in chain.nodes if node.stage == "trigger")
        if label == "primary" and trace.attribution_status == "confirmed":
            valid_trigger = any(
                (source := snapshot.sources[source_id]).kind == "event_evidence"
                and bool(source.url and source.url.strip())
                and source.occurred_at is not None
                and source.occurred_at <= snapshot.captured_at
                for source_id in trigger.evidence_ids
            )
            if not valid_trigger:
                raise ValueError("confirmed trigger requires traceable event evidence")
        observable = next(node for node in chain.nodes if node.stage == "observable_result")
        if not any(
            source_id in primary_fact_ids and snapshot.sources[source_id].kind == "market_fact"
            for source_id in observable.evidence_ids
        ):
            raise ValueError(
                f"{label} observable_result must reference primary phenomenon fact_ids"
            )

    # ── prediction_validation 校验 ──
    morning_forecast = snapshot.morning_forecast
    pv = trace.prediction_validation

    if morning_forecast is not None:
        if pv is None:
            raise ValueError(
                "prediction_validation 不得为 None：snapshot.morning_forecast 非空时必须输出预判对照"
            )
        if pv.status == "no_forecast":
            raise ValueError(
                "prediction_validation.status 不得为 no_forecast：morning_forecast 非空"
            )
        if pv.status in {"hit", "partial", "miss"} and len(pv.sector_hits) == 0:
            raise ValueError(
                f"prediction_validation.status={pv.status} 时 sector_hits 不得为空"
            )
    else:
        # morning_forecast 为空时，pv 必须为 None 或 status=no_forecast
        if pv is not None and pv.status != "no_forecast":
            raise ValueError(
                "prediction_validation.status 必须为 no_forecast：snapshot.morning_forecast 为空"
            )
        if pv is not None and (len(pv.sector_hits) > 0 or len(pv.event_hits) > 0):
            raise ValueError(
                "prediction_validation.status=no_forecast 时 sector_hits/event_hits 必须为空"
            )


# ============================================================================
# Markdown 渲染 — 从已验证的 JSON 工件渲染展示层
# ============================================================================


def _extract_sectors_from_snapshot(snapshot: MarketTraceSnapshot) -> list[str]:
    """从 snapshot.a_share.sectors 提取板块名（去重保序）。

    a_share.sectors 结构：
      {"top_gainers": [{"name": "...", ...}, ...],
       "top_losers":  [{"name": "...", ...}, ...],
       "top_inflows":  [{"name": "...", ...}, ...],
       "top_outflows": [{"name": "...", ...}, ...]}
    """
    sectors_raw = snapshot.a_share.get("sectors")
    if not isinstance(sectors_raw, dict):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for key in ("top_gainers", "top_losers", "top_inflows", "top_outflows"):
        items = sectors_raw.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _format_source_time(source: SourceRecord) -> str:
    """格式化 SourceRecord.occurred_at 为可读时间。"""
    if source.occurred_at is None:
        return "未知时间"
    try:
        return source.occurred_at.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(source.occurred_at)


def _render_candidate(candidate: CandidateExplanation, indent: str = "") -> list[str]:
    """渲染单个 CandidateExplanation 为 Markdown 行列表。"""
    lines: list[str] = []
    lines.append(f"{indent}### {candidate.category}（{candidate.status}）")
    lines.append(f"{indent}- 结论：{candidate.verdict}")
    if candidate.chain:
        for node in candidate.chain.nodes:
            lines.append(f"{indent}- **{node.stage}**：{node.claim}")
            if node.evidence_ids:
                lines.append(f"{indent}  - 证据：{', '.join(node.evidence_ids)}")
    if candidate.supporting_evidence_ids:
        lines.append(f"{indent}- 支持证据：{', '.join(candidate.supporting_evidence_ids)}")
    if candidate.counter_evidence_ids:
        lines.append(f"{indent}- 反证：{', '.join(candidate.counter_evidence_ids)}")
    return lines


def _collect_referenced_source_ids(trace: MarketTraceResult) -> list[str]:
    """收集 trace 中所有引用过的 source_id（按出现顺序去重）。"""
    referenced: list[str] = []
    seen: set[str] = set()
    for candidate in trace.candidates:
        for sid in candidate.supporting_evidence_ids + candidate.counter_evidence_ids:
            if sid not in seen:
                seen.add(sid)
                referenced.append(sid)
        if candidate.chain:
            for node in candidate.chain.nodes:
                for sid in node.evidence_ids:
                    if sid not in seen:
                        seen.add(sid)
                        referenced.append(sid)
    return referenced


def render_market_trace_markdown(
    trace: MarketTraceResult,
    snapshot: MarketTraceSnapshot,
) -> str:
    """从已验证的 trace + snapshot 渲染展示层 Markdown。

    Markdown 是展示层，不是事实源。固定章节来自 brief Step 4。
    """
    candidate_by_id = {candidate.id: candidate for candidate in trace.candidates}
    primary_candidate = candidate_by_id.get(trace.primary_chain_id or "")
    lines: list[str] = []
    lines.append(f"# A股收盘溯源｜{snapshot.trade_date}")
    lines.append(f"快照编号：{snapshot.snapshot_id}")
    lines.append("")

    lines.append("## 确认的市场现象")
    discovery = snapshot.phenomenon_discovery
    if discovery.status == "detected" and discovery.primary is not None:
        phenomenon = discovery.primary
        lines.append(f"- 类型：{phenomenon.kind}")
        lines.append(f"- 摘要：{phenomenon.summary}")
        lines.append(f"- 严重度：{phenomenon.severity}")
        lines.append(f"- 事实 ID：{', '.join(phenomenon.fact_ids)}")
    elif discovery.status == "no_phenomenon":
        lines.append("- 行情完整，未发现显著市场现象")
    else:
        lines.append("- 行情数据不足，无法可靠判断市场现象")
    lines.append("")

    lines.append("## 归因结论")
    if trace.attribution_status == "confirmed" and primary_candidate is not None:
        lines.extend(_render_candidate(primary_candidate))
    elif trace.attribution_status == "not_applicable":
        lines.append("- 不适用因果归因。")
    elif discovery.status == "insufficient_data":
        lines.append("- 行情数据不足，无法可靠判断市场现象")
    else:
        lines.append("- 证据不足，未确认主因。")
    lines.append("")

    # 预判对照章节
    pv = trace.prediction_validation
    lines.append("## 预判对照")
    if pv is None or pv.status == "no_forecast":
        lines.append("无晨报预测可对照。")
    else:
        status_map = {"hit": "全部命中", "partial": "部分命中", "miss": "全部偏离"}
        lines.append(f"- 对照状态：{status_map.get(pv.status, pv.status)}")
        if pv.sector_hits:
            lines.append("- 板块方向对照：")
            for hit in pv.sector_hits:
                result_text = "命中" if hit.result == "hit" else "偏离"
                line = f"  - {hit.sector}：晨报看{hit.morning_direction}，实际{hit.actual_direction}，{result_text}"
                if hit.result == "miss" and hit.deviation_note:
                    line += f"（原因：{hit.deviation_note}）"
                lines.append(line)
        if pv.event_hits:
            lines.append("- 事件影响对照：")
            for hit in pv.event_hits:
                lines.append(f"  - {hit.event_title}：预期{hit.morning_direction}，实际{hit.actual_impact}，{hit.result}")
        if pv.overall_note:
            lines.append(f"- 整体结论：{pv.overall_note}")
    lines.append("")

    lines.append("## 候选解释与反证")
    if trace.candidates:
        for candidate in trace.candidates:
            lines.extend(_render_candidate(candidate))
    else:
        lines.append("- 无。")
    lines.append("")

    lines.append("## 缺失证据")
    if snapshot.missing_fields:
        for field in snapshot.missing_fields:
            lines.append(f"- {field}")
    else:
        lines.append("- 无。")
    lines.append("")

    lines.append("## 证据索引")
    referenced_ids = _collect_referenced_source_ids(trace)
    if discovery.primary is not None:
        referenced_ids = list(dict.fromkeys(discovery.primary.fact_ids + referenced_ids))
    if referenced_ids:
        for sid in referenced_ids:
            source = snapshot.sources.get(sid)
            if source:
                occurred = _format_source_time(source)
                url = source.url or "无 URL"
                lines.append(f"- [{sid}] {source.provider}｜{source.title}｜{occurred}｜{url}")
    else:
        lines.append("- 无引用证据。")
    lines.append("")

    lines.append("## 未解问题")
    if trace.unresolved_questions:
        for q in trace.unresolved_questions:
            lines.append(f"- {q}")
    else:
        lines.append("- 无。")
    lines.append("")

    # 板块列表（保留 SECTOR_LIST 标记，供 _build_review_report 提取）
    lines.append("<!--SECTOR_LIST_START-->")
    for name in _extract_sectors_from_snapshot(snapshot):
        lines.append(f"- {name}")
    lines.append("<!--SECTOR_LIST_END-->")

    return "\n".join(lines)


# ============================================================================
# 持久化辅助 — Task 5：先归档事实，再缓存/持久化
# ============================================================================

# --- markdown 解析辅助：纯文本正则，不引入 LLM 调用 ---
# 主导现象（新 markdown 格式：render_market_trace_markdown 产出的 ## 主导现象 段，
# 标题行之后到下一个 '## ' 标题前的内容作为摘要来源）
_DOMINANT_PHENOMENON_RE = re.compile(
    r"##\s*(?:确认的市场现象|主导现象)[^\n]*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
# 步骤4：输出核心结论（旧 markdown 格式，保留以兼容已缓存的旧报告）
_STEP_FOUR_RE = re.compile(
    r"##\s*步骤?\s*4[：:：]?\s*输出核心结论[^\n]*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
# 步骤5 内显式标记的板块列表
_SECTOR_LIST_RE = re.compile(
    r"<!--\s*SECTOR_LIST_START\s*-->(.*?)<!--\s*SECTOR_LIST_END\s*-->",
    re.DOTALL,
)
# 附录B 板块表现矩阵：跳过表头与分隔行，取数据行第一列（板块名称）
_APPENDIX_B_RE = re.compile(
    r"##\s*附录\s*B[：:：]?\s*板块表现矩阵[^\n]*\n"
    r"(?:\|[^\n]*\|\n)?"  # 可选的表头行
    r"(?:\s*\|?\s*[-: ]+\s*\|[^\n]*\n)?"  # 分隔行（|---|---|...）
    r"((?:\|[^\n]+\n?)+)",
)


def _first_effective_line(text: str) -> str:
    """从一段文本中取首个非空、非markdown符号的有效行作为摘要。"""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*#>").strip()
        if line:
            return line
    return ""


def _extract_trace_summary(markdown: str) -> str:
    """从复盘 markdown 提取摘要（主导现象段的可读摘要）。

    摘要提取顺序：
    1. ``## 确认的市场现象`` 段落中 ``- 摘要：xxx`` 行的内容（新 markdown 格式，
       render_market_trace_markdown 产出；该行是 LLM 生成的现象描述，易读中文，
       约 15-30 字，符合晨报结论字数参考）
    2. 回退：``## 主导现象`` 段落的首个有效行（旧格式或摘要行缺失）
    3. ``## 步骤4`` 段落的首个有效行（旧 markdown 格式，兼容已缓存的旧报告）
    4. 整段 markdown 的首个有效行（兜底，避免下游拿到空字符串）
    """
    summary = ""
    m = _DOMINANT_PHENOMENON_RE.search(markdown)
    if m:
        section = m.group(1)
        # 优先取 "- 摘要：xxx" 行内容（LLM 生成的现象描述，避免取到
        # "- 类型：broad_rally" 这类内部字段行）
        summary_match = re.search(r"^\s*-\s*摘要[：:]\s*(.+)$", section, re.MULTILINE)
        if summary_match:
            summary = _first_effective_line(summary_match.group(1))
        if not summary:
            summary = _first_effective_line(section)
    if not summary:
        m = _STEP_FOUR_RE.search(markdown)
        if m:
            summary = _first_effective_line(m.group(1))
    if not summary:
        summary = _first_effective_line(markdown)
    return summary


def _extract_review_sectors(markdown: str) -> list[str]:
    """从 markdown 提取板块列表：优先 SECTOR_LIST 标记；退化到附录B 表格第一列。"""
    m = _SECTOR_LIST_RE.search(markdown)
    if m:
        sectors: list[str] = []
        for line in m.group(1).splitlines():
            name = line.strip().lstrip("-*").strip()
            if name:
                sectors.append(name)
        if sectors:
            return sectors

    m = _APPENDIX_B_RE.search(markdown)
    if m:
        sectors = []
        for row in m.group(1).splitlines():
            # 表格行形如 "| 黄金 | +3.5% | ..."
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if cells and cells[0] and cells[0] != "板块名称":
                sectors.append(cells[0])
        if sectors:
            return sectors

    return []


def _build_review_report(artifact: ReviewArtifact) -> dict[str, object]:
    """把已验证的 ReviewArtifact 封装成 schema v2 的持久化结构。

    schema v2：display_report 提供前端直接消费的字段，details 保留原始 markdown，
    market_trace 提供完整的事实快照与归因 trace，供下游快照构建器复用。
    不触发任何 LLM 调用——所有摘要/板块均从 artifact 中直接取值。
    """
    content: dict[str, object] = {
        "display_report": {
            "summary": artifact.trace_summary,
            "details": artifact.markdown,
            "stocks": [],
            "sectors": artifact.sectors,
            "risks": artifact.trace.unresolved_questions,
        },
        "podcast_brief": "",
        "schema_version": "2.0",
        "snapshot_id": artifact.snapshot.snapshot_id,
        "market_trace": {
            "snapshot": artifact.snapshot.model_dump(mode="json"),
            "trace": artifact.trace.model_dump(mode="json"),
        },
    }
    return content


async def _persist_review_report(
    state: AgentState,
    artifact: ReviewArtifact,
) -> None:
    """按 schema v2 把复盘写入 Node 端 analysis_reports；仅 scheduler 触发时写库。

    任何持久化异常都只打日志、不向上抛，保证复盘主流程的返回值不受影响。
    """
    if state.get("trigger_source") not in {"scheduler", "manual"}:
        return
    try:
        report_date = state.get("report_date") or shanghai_today().isoformat()
        content = _build_review_report(artifact)
        await node_api.save_analysis_report(
            report_type="review",
            report_date=report_date,
            data_source="review_agent",
            content=content,
        )
    except Exception as e:
        logger.warning(
            "review_persist_failed",
            error=str(e),
            exc_info=True,
        )


# ============================================================================
# 主入口
# ============================================================================


async def run(state: AgentState) -> dict[str, object]:
    """收盘溯源：冻结事实 → 归档事实 → 单次 LLM 推理 → 校验 → 渲染 → 归档/缓存/持久化。

    严格顺序（未命中路径）：
    1. build_market_trace_snapshot(report_date) 冻结事实
    2. archive_market_trace_snapshot(snapshot) 归档不可变 facts.json
    3. get_deep_think().ainvoke → 解析 → 跨对象校验
    4. render_market_trace_markdown 渲染展示层
    5. archive_review(markdown, snapshot_id) 归档复盘报告（返回 bool）
    6. set_cached_review(report_date, artifact) 缓存完整工件（返回 bool）
    7. save_analysis_report 持久化到 DB

    缓存命中路径：除 ReviewArtifact.model_validate 外，还要重新执行
    validate_trace_against_snapshot 跨对象校验，并校验缓存日期与快照日期一致；
    任一不通过视为未命中，走完整路径。缓存命中时不请求 yfinance、财联社、
    Tavily 或 LLM。命中后基于 artifact.trace + artifact.snapshot 重新调用
    render_market_trace_markdown 重建展示层（markdown / trace_summary / sectors），
    不直接使用缓存里的展示层文本，避免旧缓存或污染缓存绕过冻结快照渲染。

    任一前置步骤失败都返回降级文本，不跳到后一步。
    """
    report_date = state.get("report_date") or shanghai_today().isoformat()

    # 1. 缓存检查（命中则校验工件 + 跨对象校验 + 日期一致、持久化、返回）
    # state.skip_cache 为真时跳过缓存，强制完整流水线（管理员手动触发用）
    if not state.get("skip_cache"):
        cached = await get_cached_review(report_date)
    else:
        cached = None
        logger.info("review_skip_cache", report_date=report_date)
    if cached is not None:
        artifact: ReviewArtifact | None = None
        try:
            artifact = ReviewArtifact.model_validate(cached)
        except Exception:
            logger.debug("cached_review_artifact_invalid", exc_info=True)
            artifact = None
        # 缓存命中后必须重新执行语义校验 + 校验缓存日期与快照日期一致，
        # 防止缓存里存了旧日期/非法语义的 artifact 被当作今日报告返回。
        if artifact is not None:
            try:
                if artifact.snapshot.trade_date != report_date:
                    raise ValueError(
                        f"cached snapshot trade_date {artifact.snapshot.trade_date} "
                        f"!= report_date {report_date}"
                    )
                validate_trace_against_snapshot(artifact.trace, artifact.snapshot)
            except Exception:
                logger.warning(
                    "cached_review_artifact_semantic_invalid",
                    report_date=report_date,
                    exc_info=True,
                )
                artifact = None
        if artifact is not None:
            # P1：缓存命中不得绕过冻结快照渲染。
            # artifact.markdown / trace_summary / sectors 是缓存里的展示层文本，
            # 可能被旧缓存或污染缓存改写。这里基于 artifact.trace + artifact.snapshot
            # 重新调用 render_market_trace_markdown，以冻结 snapshot 为事实来源重建
            # 展示层字段，再传给 _persist_review_report 并返回。
            # 该路径禁止请求 Node 收盘数据、yfinance、财联社、Tavily 或 LLM；
            # 也不重写 Redis 缓存（命中即用，重写无收益且会增加风险）。
            rendered_markdown = render_market_trace_markdown(artifact.trace, artifact.snapshot)
            rebuilt_artifact = ReviewArtifact(
                schema_version=artifact.schema_version,
                snapshot=artifact.snapshot,
                trace=artifact.trace,
                markdown=rendered_markdown,
                trace_summary=_extract_trace_summary(rendered_markdown),
                sectors=_extract_review_sectors(rendered_markdown),
            )
            await _persist_review_report(state, rebuilt_artifact)
            return {"final_response": rendered_markdown}
        # 缓存内容无效（如旧纯文本、日期不一致或语义非法），视为未命中，继续走完整路径

    # 2. 冻结事实快照（必须在 LLM 调用前）
    try:
        snapshot = await build_market_trace_snapshot(report_date)
    except Exception as e:
        logger.error(
            "review_snapshot_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 3. 先校验冻结快照，再归档不可变事实（保证非法来源映射不落盘）
    try:
        validate_snapshot_discovery(snapshot)
    except ValueError as e:
        logger.error("review_discovery_validation_failed", error=str(e), exc_info=True)
        return {"final_response": DEGRADED_RESPONSE}

    try:
        archive_market_trace_snapshot(snapshot)
    except Exception as e:
        logger.error(
            "review_archive_snapshot_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": DEGRADED_RESPONSE}

    discovery_status = snapshot.phenomenon_discovery.status
    if discovery_status in {"no_phenomenon", "insufficient_data"}:
        trace = MarketTraceResult(
            schema_version="1.1",
            attribution_status=(
                "not_applicable" if discovery_status == "no_phenomenon" else "insufficient"
            ),
            candidates=[],
            primary_chain_id=None,
            alternative_chain_id=None,
            confidence="low",
            unresolved_questions=_readiness_questions(
                snapshot.phenomenon_discovery,
                snapshot.missing_fields,
            ),
        )
    else:
        trace = None

    # 4. detected 时单次 LLM 调用 + 解析 + 跨对象校验
    try:
        if trace is None:
            llm = get_deep_think()
            snapshot_json = snapshot.model_dump_json(indent=2)
            messages = [
                SystemMessage(content=REVIEW_PROMPT),
                HumanMessage(content=snapshot_json),
            ]
            ai_message = await llm.ainvoke(messages)
            raw_text = (
                ai_message.content
                if isinstance(ai_message.content, str)
                else str(ai_message.content)
            )
            cleaned = _strip_code_fences(raw_text)
            trace = MarketTraceResult.model_validate_json(
                _normalize_llm_trace_json(cleaned)
            )
        validate_trace_against_snapshot(trace, snapshot)
    except Exception as e:
        logger.error(
            "review_trace_validation_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 5. 渲染 Markdown + 构造 ReviewArtifact
    markdown = render_market_trace_markdown(trace, snapshot)
    artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=snapshot,
        trace=trace,
        markdown=markdown,
        trace_summary=_extract_trace_summary(markdown),
        sectors=_extract_review_sectors(markdown),
    )

    # 6. 归档复盘报告（仅在 facts.json 存在时创建 Markdown）
    #    严格失败顺序：归档失败 → 返回降级，不写 Redis / DB。
    if not archive_review(markdown, snapshot.snapshot_id):
        logger.error(
            "review_archive_review_failed",
            agent="review",
            snapshot_id=snapshot.snapshot_id,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 7. 缓存完整工件（model_dump(mode="json") 保证 JSON 可序列化）
    #    严格失败顺序：缓存失败 → 返回降级，不写 DB。
    if not await set_cached_review(report_date, artifact.model_dump(mode="json")):
        logger.error(
            "review_cache_set_failed",
            agent="review",
            report_date=report_date,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 8. 持久化到 DB（仅 scheduler 触发）
    await _persist_review_report(state, artifact)

    return {"final_response": markdown}


# ============================================================================
# run_review — 事件驱动直接调用入口（Gap 2）
# ============================================================================


async def run_review(
    *,
    report_date: str,
    snapshot_kind: Literal["quick", "full"],
    trace_id: str,
) -> ReviewRunResult:
    """收盘溯源直接调用入口（供 scheduler 和 trigger 端点使用）。

    与 run(state) 的区别：
    - run(state) 是 LangGraph 节点入口，返回 {"final_response": markdown}
    - run_review 是直接调用入口，返回 ReviewRunResult（含 status/metadata）

    snapshot_kind 决定使用 quick 还是 full 快照：
    - quick: 调用 build_quick_snapshot（腾讯实时行情，15:30 可用）
    - full:  调用 build_market_trace_snapshot（Tushare 完整数据，20:30 可用）

    覆盖逻辑：snapshot_kind="quick" 时先检查是否已有 full 报告。
    如果已有 full，跳过持久化（quick 不覆盖 full），返回 status="skipped"。
    """
    logger.info(
        "run_review_start",
        report_date=report_date,
        snapshot_kind=snapshot_kind,
        trace_id=trace_id,
    )

    # 覆盖检查：quick 时如果已有 full 报告，跳过
    if snapshot_kind == "quick":
        existing = await node_api.get_analysis_report("review", report_date)
        if _is_full_report(existing):
            logger.info(
                "run_review_skipped_quick_overridden_by_full",
                report_date=report_date,
                trace_id=trace_id,
            )
            return ReviewRunResult(
                status="skipped",
                report_date=report_date,
                snapshot_kind=snapshot_kind,
                trace_id=trace_id,
                markdown="",
            )

    # 选择快照构建函数
    if snapshot_kind == "quick":
        from aistock_agent.services.market_trace_snapshot import (
            build_quick_snapshot,
        )

        build_fn = build_quick_snapshot
    else:
        from aistock_agent.services.market_trace_snapshot import (
            build_market_trace_snapshot,
        )

        build_fn = build_market_trace_snapshot

    # 冻结事实快照
    try:
        snapshot = await build_fn(report_date)
    except Exception as e:
        logger.error(
            "run_review_snapshot_failed",
            error=str(e),
            exc_info=True,
            trace_id=trace_id,
        )
        return ReviewRunResult(
            status="degraded",
            report_date=report_date,
            snapshot_kind=snapshot_kind,
            trace_id=trace_id,
            markdown=DEGRADED_RESPONSE,
        )

    # 校验 + 归档
    try:
        validate_snapshot_discovery(snapshot)
    except ValueError as e:
        logger.error(
            "run_review_discovery_validation_failed",
            error=str(e),
            exc_info=True,
            trace_id=trace_id,
        )
        return ReviewRunResult(
            status="degraded",
            report_date=report_date,
            snapshot_kind=snapshot_kind,
            trace_id=trace_id,
            markdown=DEGRADED_RESPONSE,
        )

    try:
        archive_market_trace_snapshot(snapshot)
    except Exception as e:
        logger.error(
            "run_review_archive_snapshot_failed",
            error=str(e),
            exc_info=True,
            trace_id=trace_id,
        )
        return ReviewRunResult(
            status="degraded",
            report_date=report_date,
            snapshot_kind=snapshot_kind,
            trace_id=trace_id,
            markdown=DEGRADED_RESPONSE,
        )

    # discovery 为空时构造空 trace
    discovery_status = snapshot.phenomenon_discovery.status
    if discovery_status in {"no_phenomenon", "insufficient_data"}:
        trace = MarketTraceResult(
            schema_version="1.1",
            attribution_status=(
                "not_applicable" if discovery_status == "no_phenomenon" else "insufficient"
            ),
            candidates=[],
            primary_chain_id=None,
            alternative_chain_id=None,
            confidence="low",
            unresolved_questions=_readiness_questions(
                snapshot.phenomenon_discovery,
                snapshot.missing_fields,
            ),
        )
    else:
        trace = None

    # LLM 推理 + 校验
    try:
        if trace is None:
            llm = get_deep_think()
            snapshot_json = snapshot.model_dump_json(indent=2)
            messages = [
                SystemMessage(content=REVIEW_PROMPT),
                HumanMessage(content=snapshot_json),
            ]
            ai_message = await llm.ainvoke(messages)
            raw_text = (
                ai_message.content
                if isinstance(ai_message.content, str)
                else str(ai_message.content)
            )
            cleaned = _strip_code_fences(raw_text)
            trace = MarketTraceResult.model_validate_json(
                _normalize_llm_trace_json(cleaned)
            )
        validate_trace_against_snapshot(trace, snapshot)
    except Exception as e:
        logger.error(
            "run_review_trace_validation_failed",
            error=str(e),
            exc_info=True,
            trace_id=trace_id,
        )
        return ReviewRunResult(
            status="degraded",
            report_date=report_date,
            snapshot_kind=snapshot_kind,
            trace_id=trace_id,
            markdown=DEGRADED_RESPONSE,
        )

    # 渲染 + 构造 artifact
    markdown = render_market_trace_markdown(trace, snapshot)
    artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=snapshot,
        trace=trace,
        markdown=markdown,
        trace_summary=_extract_trace_summary(markdown),
        sectors=_extract_review_sectors(markdown),
    )

    # 归档 + 缓存
    if not archive_review(markdown, snapshot.snapshot_id):
        logger.error(
            "run_review_archive_review_failed",
            snapshot_id=snapshot.snapshot_id,
            trace_id=trace_id,
        )
        return ReviewRunResult(
            status="degraded",
            report_date=report_date,
            snapshot_kind=snapshot_kind,
            trace_id=trace_id,
            markdown=DEGRADED_RESPONSE,
        )

    if not await set_cached_review(report_date, artifact.model_dump(mode="json")):
        logger.error(
            "run_review_cache_set_failed",
            report_date=report_date,
            trace_id=trace_id,
        )
        return ReviewRunResult(
            status="degraded",
            report_date=report_date,
            snapshot_kind=snapshot_kind,
            trace_id=trace_id,
            markdown=DEGRADED_RESPONSE,
        )

    # 持久化到 DB
    try:
        content = _build_review_report(artifact)
        await node_api.save_analysis_report(
            report_type="review",
            report_date=report_date,
            data_source=f"review_agent_{snapshot_kind}",
            content=content,
        )
    except Exception as e:
        logger.warning(
            "run_review_persist_failed",
            error=str(e),
            exc_info=True,
            trace_id=trace_id,
        )

    logger.info(
        "run_review_done",
        status="ok",
        report_date=report_date,
        snapshot_kind=snapshot_kind,
        trace_id=trace_id,
    )
    return ReviewRunResult(
        status="ok",
        report_date=report_date,
        snapshot_kind=snapshot_kind,
        trace_id=trace_id,
        markdown=markdown,
    )


def _is_full_report(report: dict[str, object] | None) -> bool:
    """检查已存在的 review 报告是否为 full 版（data_source 含 'full'）。"""
    if not report or not isinstance(report, dict):
        return False
    data_source = str(report.get("data_source") or "")
    return "full" in data_source
