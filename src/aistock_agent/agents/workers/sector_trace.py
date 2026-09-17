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

# 板块提取来源标记（Task 9.1 三级兜底）：仅 primary_claim 为主链命中（正常依据），
# 其余为弱依据——归因链与级联预判据此标注，不把兜底当主因。
SOURCE_PRIMARY_CLAIM = "primary_claim"
SOURCE_CANDIDATE_CLAIM = "candidate_claim"
SOURCE_SNAPSHOT = "snapshot"


@dataclass
class SectorTraceRunResult:
    report_type: str = "sector_trace"
    report_date: str = ""
    sector: str = ""
    trace_result: dict[str, object] = field(default_factory=dict)
    # Spec D 级联预判：溯源快照随结果返回（SectorTraceConsumer 作为 predict_sector
    # 的 sector_snapshot 输入——板块行情 market_fact + 事件证据来源）。
    snapshot: dict[str, object] = field(default_factory=dict)
    # 父链引用（写进溯源报告 content["attribution_parent"] 的同一份）；由链组装消费
    # （Task 2.2 修"只写不读"：报告与链路同键 (report_type, report_date) 会被多板块
    # 互相覆盖，回读无法区分板块，故写入侧携带）。
    attribution_parent: dict[str, object] = field(default_factory=dict)
    # 板块提取来源/弱标记（Task 9.1）：SectorTraceConsumer 消费 extract_primary_sectors
    # 的 SectorHit 后写入（{"source": ..., "weak": ...}），归因链据此标注弱依据。
    extraction: dict[str, object] = field(default_factory=dict)


def _chain_claims(chain: object) -> list[str]:
    """链对象各节点 claim 文本（保持节点原序）。"""
    if not isinstance(chain, dict):
        return []
    raw_nodes = chain.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    return [str(n.get("claim") or "") for n in nodes if isinstance(n, dict)]


def _candidates(trace: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(trace, dict):
        return []
    raw_candidates = trace.get("candidates")
    return [c for c in raw_candidates if isinstance(c, dict)] if isinstance(
        raw_candidates, list
    ) else []


def _primary_chain_claims(trace: dict[str, object] | None) -> list[str]:
    """从 MarketTraceResult 序列化提取 primary 链各节点 claim 文本。"""
    primary_id = trace.get("primary_chain_id") if isinstance(trace, dict) else None
    for candidate in _candidates(trace):
        if candidate.get("id") != primary_id:
            continue
        return _chain_claims(candidate.get("chain"))
    return []


def _candidate_chain_claims(trace: dict[str, object] | None) -> list[str]:
    """全部候选链（含 status=weak）节点 claim 文本，保持候选与节点原序（T2 输入）。"""
    claims: list[str] = []
    for candidate in _candidates(trace):
        claims.extend(_chain_claims(candidate.get("chain")))
    return claims


@dataclass(frozen=True)
class SectorHit:
    """板块提取命中项：板块名 + 快照行 + 来源标记。

    来源取值 ``primary_claim``（主链 claim 命中，正常依据）/ ``candidate_claim``
    （候选链 claim 命中）/ ``snapshot``（快照头部队列兜底）——后两者为弱依据
    （无主链时降级所得，下游须标注，见 weak）。
    """

    name: str
    row: dict[str, object]
    source: str = SOURCE_PRIMARY_CLAIM

    @property
    def weak(self) -> bool:
        """弱依据标记：非主链命中即弱（T2/T3 兜底）。"""
        return self.source != SOURCE_PRIMARY_CLAIM


def _sector_rows(
    market_trace: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """快照板块行情两桶（top_losers / top_gainers），非 list/非 dict 项一并丢弃。"""
    snapshot = market_trace.get("snapshot")
    a_share = snapshot.get("a_share") if isinstance(snapshot, dict) else None
    sectors = a_share.get("sectors") if isinstance(a_share, dict) else None

    def _rows(raw: object) -> list[dict[str, object]]:
        return [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else []

    return (
        _rows(sectors.get("top_losers") if isinstance(sectors, dict) else []),
        _rows(sectors.get("top_gainers") if isinstance(sectors, dict) else []),
    )


def _claim_hits(
    claims: list[str],
    top_losers: list[dict[str, object]],
    top_gainers: list[dict[str, object]],
    *,
    source: str,
    seen: set[str],
    max_sectors: int,
) -> list[SectorHit]:
    """claim 命中板块名收集（claims 顺序优先，同一 claim 内跌市 losers 优先于涨市）。"""
    out: list[SectorHit] = []
    for claim in claims:
        for row in [*top_losers, *top_gainers]:
            name = str(row.get("name") or "")
            if name and name in claim and name not in seen:
                seen.add(name)
                out.append(SectorHit(name=name, row=row, source=source))
                if len(out) >= max_sectors:
                    return out
    return out


def _snapshot_hits(
    top_losers: list[dict[str, object]],
    top_gainers: list[dict[str, object]],
    *,
    seen: set[str],
    max_sectors: int,
) -> list[SectorHit]:
    """快照头部队列兜底（T3）：跌市 top_losers 优先，不足补涨市 top_gainers 头部。"""
    out: list[SectorHit] = []
    for row in [*top_losers, *top_gainers]:
        name = str(row.get("name") or "")
        if name and name not in seen:
            seen.add(name)
            out.append(SectorHit(name=name, row=row, source=SOURCE_SNAPSHOT))
            if len(out) >= max_sectors:
                return out
    return out


def extract_primary_sectors(payload: dict[str, object], max_sectors: int = 3) -> list[SectorHit]:
    """从 review 报告确定性提取主驱动板块集合（spec P1a-1：单→多 + Task 9.1 三级兜底）。

    输入形态同原 extract_primary_sector（payload={"report": Node行}）。仅在上一级
    无产出时降级（弱归因日 2026-09-17：primary_chain_id 为空且候选全 weak → 主链
    零命中导致溯源整链空转）：

    - T1 主链 claim 命中 → source=primary_claim（正常依据，行为逐字不变）；
    - T2 候选链（含 status=weak）claim 命中 → source=candidate_claim（弱）；
    - T3 快照头部队列兜底（losers → gainers）→ source=snapshot（弱）。

    逐级收集时跨层/跨来源按板块名去重（同名只收最先命中的来源），上限
    max_sectors；三层皆空返回 []。弱依据由调用方（SectorTraceConsumer →
    assemble_attribution_chain）标注，本函数只如实给出来源。
    """
    report = payload.get("report")
    if not isinstance(report, dict):
        return []
    content = report.get("content")
    market_trace = content.get("market_trace") if isinstance(content, dict) else None
    if not isinstance(market_trace, dict) or max_sectors <= 0:
        return []

    top_losers, top_gainers = _sector_rows(market_trace)
    trace = market_trace.get("trace")
    seen: set[str] = set()

    for claims, source in (
        (_primary_chain_claims(trace), SOURCE_PRIMARY_CLAIM),
        (_candidate_chain_claims(trace), SOURCE_CANDIDATE_CLAIM),
    ):
        hits = _claim_hits(
            claims,
            top_losers,
            top_gainers,
            source=source,
            seen=seen,
            max_sectors=max_sectors,
        )
        if hits:
            return hits
    return _snapshot_hits(
        top_losers, top_gainers, seen=seen, max_sectors=max_sectors
    )


def judge_sector_driver_relation(
    sector_pct: float | None, index_pct: float | None
) -> str:
    """板块驱动关系确定性判定（spec P1a-2，演示级；LLM/画像强化留 P2）。

    self_driven：板块与大盘反向，或同向但显著超（|sector| > 2*|index| + 0.5）。
    market_follow：同向且未显著超。数据缺失 → unknown。
    """
    if sector_pct is None or index_pct is None:
        return "unknown"
    if sector_pct > 0 >= index_pct or sector_pct < 0 <= index_pct:
        return "self_driven"
    if abs(sector_pct) > 2 * abs(index_pct) + 0.5:
        return "self_driven"
    return "market_follow"


def extract_primary_sector(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, object] | None]:
    """兼容旧语义：主链（T1）命中的首个（或无）。**行为不变**——不做 T2/T3 兜底。

    Task 9.1 三级兜底只作用于多板块入口 extract_primary_sectors（溯源链路）；
    单数版是"主因板块"旧语义，claim 未命中即无（不取快照桶首行兜底，见
    test_sector_trace_worker.py 既有断言）。
    """
    report = payload.get("report")
    if not isinstance(report, dict):
        return None, None
    content = report.get("content")
    market_trace = content.get("market_trace") if isinstance(content, dict) else None
    if not isinstance(market_trace, dict):
        return None, None
    top_losers, top_gainers = _sector_rows(market_trace)
    hits = _claim_hits(
        _primary_chain_claims(market_trace.get("trace")),
        top_losers,
        top_gainers,
        source=SOURCE_PRIMARY_CLAIM,
        seen=set(),
        max_sectors=1,
    )
    if not hits:
        return None, None
    return hits[0].name, hits[0].row


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
    *,
    report_date: str,
    sector_name: str,
    sector_row: dict[str, object] | None,
    parent_trace_ref: dict[str, object] | None = None,  # P1：大盘归因父链引用
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
        "schema_version": "2.1",
        "market_trace": {"snapshot": snapshot, "trace": trace_result.model_dump(mode="json")},
    }
    if parent_trace_ref:
        content["attribution_parent"] = parent_trace_ref
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
        attribution_parent=dict(parent_trace_ref or {}),
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
