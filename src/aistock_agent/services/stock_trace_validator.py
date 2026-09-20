"""Stock Trace 的确定性跨对象校验。"""

from aistock_agent.schemas.stock_trace import (
    StockSourceRecord,
    StockTraceResult,
    StockTraceSnapshot,
)
from aistock_agent.trace.chain import TRACE_CHAIN_STAGES

STAGES = TRACE_CHAIN_STAGES

# 需校验"反向事实必须引用反证"的层：(候选层, 对应快照 kind)
_COUNTER_EVIDENCE_LAYERS: tuple[tuple[str, str], ...] = (
    ("sector", "sector_fact"),
    ("market", "market_fact"),
)


class StockTraceValidationError(ValueError):
    """LLM 输出虽满足 Schema、但不满足证据或时序约束。"""


def _value_direction(source: StockSourceRecord) -> str | None:
    """从 payload 推断事实方向（与 Node `valueDirection` 口径一致）。"""
    numeric = source.payload.get("change_pct", source.payload.get("pct_change"))
    if isinstance(numeric, (int, float)) and not isinstance(numeric, bool):
        return "up" if numeric > 0 else "down" if numeric < 0 else "neutral"
    impact = str(source.payload.get("impact") or "").lower()
    if "利好" in impact or "positive" in impact:
        return "up"
    if "利空" in impact or "negative" in impact:
        return "down"
    return None


def validate_stock_trace_result(result: StockTraceResult, snapshot: StockTraceSnapshot) -> None:
    if result.event_id != snapshot.event_id or result.snapshot_id != snapshot.snapshot_id:
        raise StockTraceValidationError("result must bind the supplied event and snapshot")

    source_by_id = {source.source_id: source for source in snapshot.source_records}
    candidate_by_id = {candidate.candidate_id: candidate for candidate in result.candidates}
    for candidate in result.candidates:
        for source_id in candidate.supporting_evidence_ids + candidate.counter_evidence_ids:
            if source_id not in source_by_id:
                raise StockTraceValidationError(f"candidate references unknown source: {source_id}")

    chain_by_id = {chain.chain_id: chain for chain in result.chains}
    for chain in result.chains:
        if chain.candidate_id not in candidate_by_id:
            raise StockTraceValidationError("chain candidate must exist")
        if chain.role == "primary" and chain.chain_id != result.primary_chain_id:
            raise StockTraceValidationError("primary chain role must match primary_chain_id")
        if chain.chain_id in {result.primary_chain_id, result.alternative_chain_id}:
            if tuple(node.stage for node in chain.nodes) != STAGES:
                raise StockTraceValidationError(
                    "selected chain must contain the ordered six stages"
                )
        for node in chain.nodes:
            for source_id in node.evidence_ids + node.counter_evidence_ids:
                if source_id not in source_by_id:
                    raise StockTraceValidationError(
                        f"chain node references unknown source: {source_id}"
                    )
            if node.epistemic_type == "fact" and not node.evidence_ids:
                raise StockTraceValidationError("fact node requires evidence")
            if node.status == "not_established" and node.evidence_ids:
                raise StockTraceValidationError(
                    "not established node cannot carry positive evidence"
                )

    # 2026-09-18：镜像 Node `validateStockTraceResult` 的 missing_counter_evidence 规则——
    # 窗口内存在与个股方向相反的板块/大盘事实时，仍把该层置 supported 的候选必须引用反证。
    # 镜像的目的是让 LLM 纠错重试有机会修正；Node 侧是回写后的终态门，
    # 被拒只会变成 partial（无 artifact），没有重试机会。
    for layer, kind in _COUNTER_EVIDENCE_LAYERS:
        has_opposite_fact = any(
            source.kind == kind
            and source.occurred_at is not None
            and source.occurred_at <= snapshot.trigger_event.window_end_at
            and _value_direction(source) not in {None, "neutral", snapshot.trigger_event.direction}
            for source in snapshot.source_records
        )
        candidate = next((item for item in result.candidates if item.layer == layer), None)
        if (
            has_opposite_fact
            and candidate is not None
            and candidate.status == "supported"
            and not candidate.counter_evidence_ids
        ):
            raise StockTraceValidationError(f"candidate:{layer}:missing_counter_evidence")

    if result.primary_chain_id and result.primary_chain_id not in chain_by_id:
        raise StockTraceValidationError("primary chain does not exist")
    if result.attribution_status != "confirmed":
        return
    if result.confidence_score < 0.75 or result.confidence_level != "high":
        raise StockTraceValidationError("confirmed requires high confidence at or above 0.75")
    if not result.primary_chain_id:
        raise StockTraceValidationError("confirmed requires primary chain")
    primary = chain_by_id[result.primary_chain_id]
    candidate = candidate_by_id[primary.candidate_id]
    if candidate.layer != "company" or candidate.status != "supported":
        raise StockTraceValidationError("confirmed requires a supported company primary candidate")
    evidence = [source_by_id[source_id] for source_id in candidate.supporting_evidence_ids]
    has_a = any(source.source_level == "A" for source in evidence)
    has_b = any(source.source_level == "B" for source in evidence)
    has_independent_market_fact = any(
        source.kind == "market_fact" and source.source_level in {"A", "B"}
        for source in snapshot.source_records
    )
    if not has_a and not (has_b and has_independent_market_fact):
        raise StockTraceValidationError(
            "confirmed requires A evidence or B evidence plus market fact"
        )
    if any(source.source_level == "D" for source in evidence):
        raise StockTraceValidationError("D evidence cannot confirm a primary cause")
