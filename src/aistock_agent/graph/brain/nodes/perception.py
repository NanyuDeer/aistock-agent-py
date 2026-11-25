"""感知层最小实现（第一期只做大盘指数）。

数据源走 node_api（结构化），不解析旧工具返回的格式化字符串；
空数据一律降级留痕，不抛异常打断整条链。
"""

from aistock_agent.graph.brain.state import BrainState
from aistock_agent.schemas.judgment import JudgmentEvidence
from aistock_agent.services.data_client import node_api
from aistock_agent.tools.provenance import degraded_envelope, envelope_to_evidence, ok_envelope


async def _load_index_rows(code: str, days: int) -> list[dict[str, object]] | None:
    return await node_api.get_index_kline(code, days=days)


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _date_id(value: object) -> str:
    """交易日转证据 id 片段：去掉横线，保持 id 稳定且可读（2026-11-25 → 20261125）。"""
    return str(value).replace("-", "")


async def market_perception(state: BrainState) -> dict[str, object]:
    code = state.get("subject_code", "000001")
    subject = state.get("subject", "上证指数")
    rows = await _load_index_rows(code, 5)

    if not rows:
        envelope = degraded_envelope(
            source="internal:index_kline", source_type="market_data", error="index_kline_empty"
        )
        return {
            "trace": [{"node": "market_perception", "status": "degraded", "error": envelope.error}],
            "render_targets": ["home"],
        }

    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else rows[-1]
    close = _as_float(latest.get("close"))
    pct = _as_float(latest.get("pct_chg"))
    prev_close = _as_float(prev.get("close"))
    prev_pct = _as_float(prev.get("pct_chg"))
    high = max(_as_float(row.get("high")) for row in rows)
    drawdown = (close / high - 1) * 100 if high else 0.0

    def _wrap(
        claim: str, *, evidence_id: str, published_at: str, value: float, quality: str = "high"
    ) -> JudgmentEvidence:
        envelope = ok_envelope(
            latest,
            source="internal:index_kline",
            source_type="market_data",
            published_at=published_at,
            quality=quality,  # type: ignore[arg-type]
        )
        return envelope_to_evidence(
            envelope,
            evidence_id=evidence_id,
            kind="market",
            subject=subject,
            claim=claim,
            value=value,
            unit="%",
        )

    evidence = [
        _wrap(
            f"{subject}收 {close:.2f}（{pct:+.2f}%）",
            evidence_id=f"ev_idx_{_date_id(latest['trade_date'])}",
            published_at=str(latest["trade_date"]),
            value=pct,
        ),
        _wrap(
            f"前一交易日收 {prev_close:.2f}（{prev_pct:+.2f}%）",
            evidence_id=f"ev_idx_prev_{_date_id(prev['trade_date'])}",
            published_at=str(prev["trade_date"]),
            value=prev_pct,
        ),
        _wrap(
            f"近 5 日最高 {high:.2f}，当前回撤 {drawdown:+.2f}%",
            evidence_id=f"ev_idx_high_{_date_id(latest['trade_date'])}",
            published_at=str(latest["trade_date"]),
            value=drawdown,
            quality="medium",
        ),
    ]
    return {
        "evidence": evidence,
        "render_targets": ["home", "stock_page"],
        "trace": [{"node": "market_perception", "status": "ok", "evidence_count": len(evidence)}],
    }
