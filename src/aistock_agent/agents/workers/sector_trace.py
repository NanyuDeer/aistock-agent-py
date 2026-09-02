"""板块溯源 worker（Spec D · 溯源环 · 事件层归因）。

review_done 事件触发的板块级事件归因：对主因板块回答「今天为什么暴/大跌或
异动归因」。复用 CausalChain/ChainStage 的链结构语义；独立报告
report_type="sector_trace"。
"""
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.sector_trace import _GENERATE_SECTOR_PROMPT
from aistock_agent.schemas.sector_trace import SectorChainResult, validate_sector_chain
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.sector_trace_snapshot import build_sector_snapshot


@dataclass
class SectorTraceRunResult:
    report_type: str = "sector_trace"
    report_date: str = ""
    sector: str = ""
    trace_result: dict[str, object] = field(default_factory=dict)
    # Spec D 级联预判：溯源快照随结果返回（SectorTraceConsumer 作为 predict_sector
    # 的 sector_snapshot 输入——板块行情 market_fact + 事件证据来源）。
    snapshot: dict[str, object] = field(default_factory=dict)


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

    输入形态：payload 包一层 {"report": Node行}（D4 约定，consumer 传
    node_api.get_analysis_report 返回的 Node DB 行）。快照与 trace 均从
    content.market_trace 下读取（review._build_review_report 持久化结构）。
    primary 链 claim 命中板块行情名 → 返回 (板块名, 行情条目)：
    - 跌市主因：先命中 top_losers（原语义保持优先）；
    - 涨市主因：未命中 losers 时回退 top_gainers（2026-09-02 实盘验证：
      8.27 英伟达财报催化 AI 算力链领涨，主因板块在 top_gainers，只查 losers 会漏）；
    无命中 → 返回 (None, None)（宁缺毋滥，调用方跳过不产出）。
    """
    report = payload.get("report")
    if not isinstance(report, dict):
        return None, None
    # Node 行：content.market_trace.snapshot + trace 同级（review 持久化结构）
    content = report.get("content")
    content = content if isinstance(content, dict) else None
    market_trace = content.get("market_trace") if isinstance(content, dict) else None
    market_trace = market_trace if isinstance(market_trace, dict) else None
    snapshot = market_trace.get("snapshot") if isinstance(market_trace, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else None
    a_share = snapshot.get("a_share") if isinstance(snapshot, dict) else None
    sectors = a_share.get("sectors") if isinstance(a_share, dict) else None

    def _rows(raw: object) -> list[dict[str, object]]:
        """收窄行情桶为 dict 行列表（非 list / 非 dict 项安全过滤）。"""
        return [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else []

    top_losers = _rows(sectors.get("top_losers") if isinstance(sectors, dict) else [])
    top_gainers = _rows(sectors.get("top_gainers") if isinstance(sectors, dict) else [])

    trace = market_trace.get("trace") if isinstance(market_trace, dict) else None
    for claim in _primary_chain_claims(trace):
        for los in top_losers:
            name = str(los.get("name") or "")
            if name and name in claim:
                return name, los
        for g in top_gainers:
            name = str(g.get("name") or "")
            if name and name in claim:
                return name, g
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
    # 定向事件检索路径在 snapshot 内部走 TavilyService.search（D4.5 接线，
    # 无外部上下文注入；快照内失败静默降级语义不变）
    snapshot = await build_sector_snapshot(
        report_date=report_date,
        sector_name=sector_name,
        sector_row=sector_row,
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
        snapshot=snapshot,
    )


async def run(state: dict[str, object]) -> dict[str, object]:
    """iterate/事件链 run_entry="run" 约定：从 state 读 report_date/sector。

    返回对齐 review.run 的迭代评分消费契约（replay_runner.run_once 归因分支读取）：
    - ``final_response``：trace_result 的 JSON 串（evaluate_attribution 的
      extract_agent_attribution 消费，从 LLM 归因链文本提取方向/驱动/板块）；
    - ``sectors``：顶层确定性板块清单（run_once 转 structured 回传，sector 维度
      优先于 LLM 文本提取，对齐 evaluate_attribution 的 agent_structured 契约）。
    回放态（REPLAY）下 node 写与定向搜索已被 replay_layer 隔离（save_analysis_report
    → no-op、TavilyService.search → 空语料），本函数无需特判。
    """
    import json

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
    trace_result = res.trace_result
    return {
        "report_type": res.report_type,
        "trace_result": trace_result,
        "final_response": json.dumps(trace_result, ensure_ascii=False),
        "sectors": [str(trace_result.get("sector") or sector_name)],
    }
