"""归因链组装与保存（spec P1a-3：大盘-板块-事件 链树的 agent 侧产物）。"""
import structlog

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()


def _trace_summary(trace_result: dict[str, object]) -> str:
    for key in ("summary", "observable_result", "attribution_summary"):
        v = trace_result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "板块溯源完成"


def _pct_from(snapshot: dict[str, object]) -> float | None:
    sector = snapshot.get("sector") if isinstance(snapshot, dict) else None
    if not isinstance(sector, dict):
        return None
    v = sector.get("pct_change")
    if v is None:
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
        await self.node_api.post(
            "/internal/attribution-chain", {"date": report_date, "chain": chain}
        )
        logger.info(
            "attribution_chain.saved",
            report_date=report_date,
            children=len(chain.get("children", [])),
        )
