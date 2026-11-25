"""研究层（第一期：evidence 投影，不调 LLM）。

为什么先不接 LLM：样板链要先证明"契约 + 状态 + 出口"在真实运行下自洽；
LLM 接入是 P2 的替换点，替换时只改本文件的 insight_research，不动主图与出口。
"""

from typing import Literal

from aistock_agent.graph.brain.state import BrainState
from aistock_agent.schemas.judgment import JudgmentClaim

PrimaryCause = Literal["market", "sector", "stock"]

# 归因阈值（pct 为百分点，不是百分比数值）
_STOCK_DEVIATION = 3.0
_SECTOR_DEVIATION = 1.0


def classify_primary_cause(
    *,
    market_pct: float,
    sector_pct: float | None = None,
    stock_pct: float | None = None,
) -> PrimaryCause:
    """主因三分类：个股独立行情优先，其次板块，最后大盘。"""
    if stock_pct is not None and abs(stock_pct - market_pct) >= _STOCK_DEVIATION:
        return "stock"
    if sector_pct is not None and abs(sector_pct - market_pct) >= _SECTOR_DEVIATION:
        return "sector"
    return "market"


async def insight_research(state: BrainState) -> dict[str, object]:
    evidence = [item for item in state.get("evidence") or [] if item.kind == "market"]
    if not evidence:
        return {
            "trace": [
                {"node": "insight_research", "status": "degraded", "error": "no_market_evidence"}
            ]
        }

    latest = evidence[0]
    cause = classify_primary_cause(market_pct=latest.value)
    claim = JudgmentClaim(
        claim_id=f"clm_{latest.evidence_id}",
        subject=latest.subject,
        kind="market",
        statement=f"{latest.claim}；主因归类：{cause}",
        evidence_ids=[item.evidence_id for item in evidence],
        confidence=0.62,
        invalidated_by=["下一交易日收盘跌破近 5 日最低点"],
        produced_by="insight_research",
    )
    return {
        "claims": [claim],
        "method_prior": {"default": {"primary_cause": cause}},
        "trace": [{"node": "insight_research", "status": "ok", "primary_cause": cause}],
    }
