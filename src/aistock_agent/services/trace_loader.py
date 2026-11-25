"""报告工件读取服务：只消费已持久化且通过校验的 review 工件。"""

from datetime import date as date_type

import structlog

from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
)

logger = structlog.get_logger()


async def load_validated_trace(
    date_str: str,
) -> tuple[MarketTraceSnapshot, MarketTraceResult] | None:
    """加载并校验已持久化的 ReviewArtifact 中的 trace + snapshot。

    供 trace_lookup Skill 使用，跳过 LLM 选择步骤。
    失败时返回 None，由调用方决定如何 degraded。

    Args:
        date_str: 报告日期 YYYY-MM-DD。

    Returns:
        (snapshot, trace) 元组；报告不存在或校验失败时返回 None。
    """
    from aistock_agent.agents.workers.review import validate_trace_against_snapshot
    from aistock_agent.services.data_client import NodeApiClient

    try:
        report_date = date_type.fromisoformat(date_str)
    except ValueError:
        logger.warning("load_validated_trace_bad_date", date_str=date_str)
        return None

    try:
        client = NodeApiClient()
        read_result = await client.get_review_analysis_report(report_date)
    except Exception as exc:
        logger.warning(
            "load_validated_trace_read_failed",
            date_str=date_str,
            err=str(exc),
        )
        return None

    if read_result.status != "found" or not isinstance(read_result.report, dict):
        logger.info(
            "load_validated_trace_not_found",
            date_str=date_str,
            status=read_result.status,
        )
        return None

    report = read_result.report
    if report.get("status") != "completed":
        logger.info("load_validated_trace_not_completed", date_str=date_str)
        return None
    content = report.get("content")
    if not isinstance(content, dict):
        logger.warning("load_validated_trace_invalid_content", date_str=date_str)
        return None

    market_trace = content.get("market_trace")
    if not isinstance(market_trace, dict):
        logger.warning("load_validated_trace_invalid_market_trace", date_str=date_str)
        return None
    snapshot_data = market_trace.get("snapshot")
    trace_data = market_trace.get("trace")
    if not isinstance(snapshot_data, dict) or not isinstance(trace_data, dict):
        return None

    try:
        snapshot = MarketTraceSnapshot.model_validate(snapshot_data)
        trace = MarketTraceResult.model_validate(trace_data)
        if snapshot.trade_date != date_str:
            logger.warning(
                "load_validated_trace_date_mismatch",
                date_str=date_str,
                snapshot_date=snapshot.trade_date,
            )
            return None
        validate_trace_against_snapshot(trace, snapshot)
        return snapshot, trace
    except Exception as exc:
        logger.warning(
            "load_validated_trace_validate_failed",
            date_str=date_str,
            err=str(exc),
        )
        return None
