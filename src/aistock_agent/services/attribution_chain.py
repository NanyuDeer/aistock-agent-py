"""归因链组装与保存（spec P1a-3：大盘-板块-事件 链树的 agent 侧产物）。"""
import structlog

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

# 溯源未确认驱动原因时的 trace_summary 回退文案（区别于"溯源完成"占位——溯源
# insufficient 或无法从 stages 提取 trigger 结论时，如实说明原因未确认）。
_FALLBACK_TRACE_SUMMARY = "溯源未确认驱动原因"


def _trace_summary(trace_result: dict[str, object]) -> str:
    """从真实板块溯源 dump（SectorChainResult.model_dump(mode="json")）摘一句话。

    真实形状：{chain_id, sector, stages:[{kind, headline, claims, evidence}],
    attribution_status, missing_evidence}——没有 summary/observable_result 等
    顶层文案键。归因结论在 trigger stage（事件主因）的 headline/claims 里；
    attribution_status=insufficient 或无法提取（无 stages/无 trigger/无文本）
    时回退 _FALLBACK_TRACE_SUMMARY，避免显示"板块溯源完成"误导。
    """
    if not isinstance(trace_result, dict):
        return _FALLBACK_TRACE_SUMMARY
    if trace_result.get("attribution_status") == "insufficient":
        return _FALLBACK_TRACE_SUMMARY
    stages = trace_result.get("stages")
    if not isinstance(stages, list):
        return _FALLBACK_TRACE_SUMMARY
    trigger = next(
        (s for s in stages if isinstance(s, dict) and s.get("kind") == "trigger"),
        None,
    )
    if not isinstance(trigger, dict):
        return _FALLBACK_TRACE_SUMMARY
    headline = trigger.get("headline")
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    claims = trigger.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, str) and claim.strip():
                return claim.strip()
    return _FALLBACK_TRACE_SUMMARY


def _pct_from(snapshot: dict[str, object]) -> float | None:
    sector = snapshot.get("sector") if isinstance(snapshot, dict) else None
    if not isinstance(sector, dict):
        return None
    v = sector.get("pct_change")
    if v is None:
        # 兼容 wind-leaders 快照行（无 pct_change，只有 today_change 字段）
        v = sector.get("today_change")
    return float(v) if isinstance(v, int | float) else None


def assemble_attribution_chain(
    report_date: str,
    review_payload: dict[str, object],
    sector_results: list[object],
) -> dict[str, object]:
    """组装 大盘(market) → 主驱动板块(self_driven/follow) 归因链。"""
    report = review_payload.get("report")
    content = report.get("content") if isinstance(report, dict) else None
    content = content if isinstance(content, dict) else None
    mt = content.get("market_trace") if isinstance(content, dict) else None
    mt = mt if isinstance(mt, dict) else None
    snapshot = mt.get("snapshot") if isinstance(mt, dict) else None
    a_share = snapshot.get("a_share") if isinstance(snapshot, dict) else None
    trace = mt.get("trace") if isinstance(mt, dict) else None

    index_pct: float | None = None
    if isinstance(a_share, dict):
        for key in ("index_change_pct", "index_pct", "benchmark_change_pct", "sh_change_pct"):
            v = a_share.get(key)
            if isinstance(v, int | float):
                index_pct = float(v)
                break

    summary = str(trace.get("attribution_summary") or "") if isinstance(trace, dict) else ""

    children: list[dict[str, object]] = []
    for res in sector_results:
        sector = str(getattr(res, "sector", "") or "")
        trace_result = getattr(res, "trace_result", {}) or {}
        snapshot_dict = getattr(res, "snapshot", {}) or {}
        pct = _pct_from(snapshot_dict) if isinstance(snapshot_dict, dict) else None
        children.append(
            {
                "sector": sector,
                "relation": judge_sector_driver_relation(pct, index_pct),
                "pct": pct,
                "trace_summary": _trace_summary(trace_result),
            }
        )

    return {
        "date": report_date,
        "root": {"type": "market", "date": report_date, "summary": summary, "index_pct": index_pct},
        "children": children,
    }


class AttributionChainStore:
    def __init__(self) -> None:
        self.node_api = node_api

    async def save(self, report_date: str, chain: dict[str, object]) -> None:
        result = await self.node_api.post(
            "/internal/attribution-chain", {"date": report_date, "chain": chain}
        )
        if result is None:
            # data_client.post 失败/业务码异常吞错返回 None → 告警而非误报 saved
            logger.warning(
                "attribution_chain.save_failed",
                report_date=report_date,
                error="node_api.post 返回 None（请求失败或业务码异常）",
            )
            return
        logger.info(
            "attribution_chain.saved",
            report_date=report_date,
            children=len(chain.get("children", [])),
        )
