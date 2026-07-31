"""Stock Trace 的确定性跨对象校验。"""

from aistock_agent.schemas.stock_trace import StockTraceResult, StockTraceSnapshot

STAGES = (
    "structural_root", "trigger", "transmission", "exposure", "repricing", "observable_result"
)


class StockTraceValidationError(ValueError):
    """LLM 输出虽满足 Schema、但不满足证据或时序约束。"""


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
