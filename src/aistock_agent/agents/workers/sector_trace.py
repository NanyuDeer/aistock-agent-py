"""板块溯源 worker（Spec D · 溯源环 · 事件层归因）。

review_done 事件触发的板块级事件归因：对主因板块回答「今天为什么暴/大涨」。
复用 CausalChain/ChainStage 的链结构语义；独立报告 report_type="sector_trace"。
"""
from dataclasses import dataclass, field
from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.sector_trace import _GENERATE_SECTOR_PROMPT
from aistock_agent.schemas.sector_trace import SectorChainResult, validate_sector_chain
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.sector_trace_snapshot import (
    _SearchContext,
    build_sector_snapshot,
)


@dataclass
class SectorTraceRunResult:
    report_type: str = "sector_trace"
    report_date: str = ""
    sector: str = ""
    trace_result: dict[str, object] = field(default_factory=dict)


def _primary_chain_claims(trace: dict[str, object] | None) -> list[str]:
    """从 MarketTraceResult 序列化提取 primary 链各节点 claim 文本。"""
    if not isinstance(trace, dict):
        return []
    primary_id = trace.get("primary_chain_id")
    raw_candidates = trace.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("id") != primary_id:
            continue
        chain = candidate.get("chain")
        if not isinstance(chain, dict):
            continue
        raw_nodes = chain.get("nodes")
        nodes = raw_nodes if isinstance(raw_nodes, list) else []
        return [str(n.get("claim") or "") for n in nodes if isinstance(n, dict)]
    return []


def extract_primary_sector(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, object] | None]:
    """从 review 报告确定性提取主因板块（不依赖 LLM）。

    输入形态：payload 直接为 {"content": ..., "snapshot": ...}，或包一层
    {"report": {...}}（D4 约定）。primary 链 claim 命中 top_losers 板块名 →
    返回 (板块名, 行情条目)；回退 top_losers[0]；两者皆空返回 (None, None)
    （调用方跳过不产出）。
    """
    report = payload.get("report")
    if not isinstance(report, dict):
        report = payload
    snapshot = report.get("snapshot")
    a_share = snapshot.get("a_share") if isinstance(snapshot, dict) else None
    sectors = a_share.get("sectors") if isinstance(a_share, dict) else None
    raw_losers = sectors.get("top_losers") if isinstance(sectors, dict) else []
    losers = raw_losers if isinstance(raw_losers, list) else []
    top_losers = [t for t in losers if isinstance(t, dict)]

    content = report.get("content")
    market_trace = content.get("market_trace") if isinstance(content, dict) else None
    trace = market_trace.get("trace") if isinstance(market_trace, dict) else None
    for claim in _primary_chain_claims(trace):
        for los in top_losers:
            name = str(los.get("name") or "")
            if name and name in claim:
                return name, los
    if top_losers:
        first = top_losers[0]
        return str(first.get("name") or ""), first
    return None, None


async def _generate_sector_trace_with_retry(
    snapshot: dict[str, object], *, captured_at: str
) -> SectorChainResult:
    """LLM 事件层归因；解析失败重试一次（对齐 review._generate_trace_with_retry）。

    review 先例（review.py:184-206）：get_deep_think().ainvoke → model_validate_json
    失败重试一次 → validate_trace_against_snapshot。此处等价封装为
    SectorChainResult.model_validate_json + validate_sector_chain。
    """
    async def _attempt() -> SectorChainResult | None:
        llm = get_deep_think()
        messages = [
            SystemMessage(content=_GENERATE_SECTOR_PROMPT),
            HumanMessage(content=f"快照:\n{snapshot}"),
        ]
        text = await llm.ainvoke(messages)
        raw = str(getattr(text, "content", text))
        start, end = raw.find("{"), raw.rfind("}")
        payload = raw[start : end + 1] if start >= 0 and end > start else ""
        try:
            return SectorChainResult.model_validate_json(payload)
        except Exception:
            return None

    result = await _attempt()
    if result is None:
        result = await _attempt()
        if result is None:
            raise RuntimeError("sector_trace: LLM 归因两次解析失败")
    validate_sector_chain(result, captured_at=captured_at)
    return result


async def run_sector_trace(
    *, report_date: str, sector_name: str, sector_row: dict[str, object] | None
) -> SectorTraceRunResult:
    snapshot = await build_sector_snapshot(
        report_date=report_date,
        sector_name=sector_name,
        sector_row=sector_row,
        trace_ctx=cast(_SearchContext, node_api),
    )
    trace_result = await _generate_sector_trace_with_retry(snapshot, captured_at=report_date)
    content = {
        "display_report": {"summary": "", "sectors": [sector_name], "risks": []},
        "schema_version": "2.0",
        "market_trace": {"snapshot": snapshot, "trace": trace_result.model_dump(mode="json")},
    }
    await node_api.save_analysis_report(
        report_type="sector_trace",
        report_date=report_date,
        data_source="sector_trace_agent",
        content=content,
    )
    return SectorTraceRunResult(
        report_date=report_date,
        sector=sector_name,
        trace_result=trace_result.model_dump(mode="json"),
    )


async def run(state: dict[str, object]) -> dict[str, object]:
    """iterate/事件链 run_entry="run" 约定：从 state 读 report_date/sector。"""
    report_date = str(state.get("report_date") or "")
    sector = state.get("sector")
    if isinstance(sector, dict):
        sector_name = str(sector.get("name") or state.get("sector_name") or "")
    else:
        sector_name = str(sector or state.get("sector_name") or "")
    res = await run_sector_trace(
        report_date=report_date,
        sector_name=sector_name,
        sector_row=sector if isinstance(sector, dict) else None,
    )
    return {"report_type": res.report_type, "trace_result": res.trace_result}
