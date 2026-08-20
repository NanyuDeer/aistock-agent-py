"""事件分析流水线 — Event Conduction → Global Importance

将 Global Importance 从 09:00 broadcast 链路迁移到事件分析流水线末端，
保证 GI 的输入一定来自已完成的 event_conduction 分析内容。

职责：
    Step 1: 执行事件传导分析（run_event_conduction_batch，并行 + 单事件异常隔离）
    Step 2: 全部事件分析完成后，从 EventConductionOutput.analysis_report 提取
            完整的 analysis_reports 内容，传递给 Global Importance

约束：
    - 单事件失败/超时不影响其他事件（P0-2：per_event_timeout 单事件超时隔离，
      gather return_exceptions=True 语义由 run_event_conduction_batch 内部保证）
    - GI 输入 = 已确认落库事件（success = event_generated AND persisted，P1-2）
    - 不新增 Redis 依赖，不改变 Global Importance 输出 Schema
    - GI 输入从内存 analysis_report 提取，不查询 PostgreSQL
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from aistock_agent.config import settings
from aistock_agent.services.event_conduction import (
    AnalysisReportPayload,
    EventConductionOutput,
    run_event_conduction_batch,
)

logger = structlog.get_logger()


def _to_gi_events(
    conduction_outputs: list[EventConductionOutput],
) -> list[dict[str, object]]:
    """将当天 event_conduction 输出映射为 GI 输入格式（当天事件池）。

    从 EventConductionOutput.analysis_report 提取完整分析字段，
    仅映射成功的事件（output.status.success=True 且 analysis_report 非 None），
    过滤失败/超时事件。

    Args:
        conduction_outputs: 当天 pipeline 的传导输出列表。

    Returns:
        GI 输入结构 events[] 数组（仅含成功事件，包含完整分析内容）。
    """
    events: list[dict[str, object]] = []

    for output in conduction_outputs:
        if not output.status.success:
            continue
        report = output.analysis_report
        if report is None:
            continue

        events.append({
            "event_id": report.event_id,
            "event_time": "",
            "event_age_days": 0,
            "summary": report.summary,
            "original_event": report.original_event,
            "impact_industries": report.impact_industries,
            "impact_chain": report.impact_chain,
            "key_variables": report.key_variables,
            "mechanism": report.mechanism,
            "investment_rating": report.investment_rating,
            "investment_conclusion": report.investment_conclusion,
        })
    return events


async def run_event_analysis_pipeline(
    major_events: list[dict[str, object]],
    *,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    """事件分析流水线：事件传导完成 → Global Importance。

    Args:
        major_events: morning 生成的 major_events 列表。
        timeout_seconds: 单事件超时（秒，P0-2：由整批超时改为每个事件独立超时），
            默认读取配置 ``event_analysis_pipeline_timeout_seconds``。
            单事件超时只取消该事件，已完成事件正常进入 persist，不再整批取消。

    Returns:
        {
            "event_count": int,
            "conduction_results": list[EventConductionResult],
            "gi_result": dict | None,        # persist_global_importance_evaluation 返回
            "timed_out": bool,
            "elapsed_seconds": float,
            "error": str | None,
        }

    说明：
        - 即使部分事件失败，也尝试执行 GI（使用已确认落库的成功事件，
          success = event_generated AND persisted）。
        - 单事件超时/异常仅记录日志，不向调用方抛出，不影响 broadcast。
    """
    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.event_analysis_pipeline_timeout_seconds
    )

    logger.info("event_analysis_pipeline_started", event_count=len(major_events))

    t_start = time.monotonic()
    timed_out = False
    error: str | None = None
    conduction_outputs: list[EventConductionOutput] = []
    gi_result: dict[str, object] | None = None

    try:
        # ── Step 1: Event Conduction（并行，单事件超时 + 异常隔离） ──
        # P0-2：不再用 wait_for 包裹整个批次——超时下沉到单个事件，
        # 一个事件超时不再取消其他事件，已完成事件正常进入 persist。
        conduction_outputs = await run_event_conduction_batch(
            major_events,
            per_event_timeout=timeout,
        )
        success_count = sum(1 for o in conduction_outputs if o.status.success)
        logger.info(
            "event_conduction_completed",
            total=len(conduction_outputs),
            success=success_count,
            failed=len(conduction_outputs) - success_count,
        )

        # ── Step 2: Global Importance（输入为当天 event_conduction pipeline 结果） ──
        logger.info(
            "global_importance_started",
            event_count=len(conduction_outputs),
        )
        # 将当天传导结果转换为 GI 输入格式（当天事件池 + 完整分析内容）
        # _to_gi_events 仅收集 success=True 的事件，即已确认落库事件
        # （P1-2：GI 输入 = 已确认落库事件）。
        today_events = _to_gi_events(conduction_outputs)
        # 盘中纯增量更新（2026-08-14）：开关开启时走 incremental_gi——
        # 新增事件与当前 max_bullish/max_bearish 竞争，仅必要时 quick_think，
        # 不重新分析当天全部事件，也不新增收盘全量校准。开关关闭时保持
        # 原全量 persist_global_importance_evaluation（旧路径，仅测试/手动恢复）。
        if settings.gi_incremental_enabled:
            from aistock_agent.services.global_importance_evaluation import (  # noqa: PLC0415
                incremental_gi,
            )

            gi_result = await incremental_gi(today_events)
            logger.info(
                "global_importance_incremental_completed",
                has_top_bullish=bool(
                    gi_result.get("top_bullish_event")
                    if isinstance(gi_result, dict)
                    else False
                ),
                has_top_bearish=bool(
                    gi_result.get("top_bearish_event")
                    if isinstance(gi_result, dict)
                    else False
                ),
                persisted=bool(
                    gi_result.get("persisted") if isinstance(gi_result, dict) else False
                ),
            )
        else:
            from aistock_agent.services.global_importance_evaluation import (  # noqa: PLC0415
                persist_global_importance_evaluation,
            )

            gi_result = await persist_global_importance_evaluation(today_events)
            logger.info(
                "global_importance_completed",
                has_top_bullish=bool(
                    gi_result.get("top_bullish_event")
                    if isinstance(gi_result, dict)
                    else False
                ),
                has_top_bearish=bool(
                    gi_result.get("top_bearish_event")
                    if isinstance(gi_result, dict)
                    else False
                ),
                persisted=bool(
                    gi_result.get("persisted") if isinstance(gi_result, dict) else False
                ),
            )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        logger.exception(
            "event_analysis_pipeline_failed",
            error=error,
            event_count=len(major_events),
        )

    elapsed = round(time.monotonic() - t_start, 2)
    logger.info(
        "event_analysis_pipeline_finished",
        event_count=len(major_events),
        elapsed_seconds=elapsed,
        timed_out=timed_out,
        error=error,
    )

    return {
        "event_count": len(major_events),
        "conduction_results": [
            _result_to_dict(o) for o in conduction_outputs
        ],
        "gi_result": gi_result,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "error": error,
    }


def _result_to_dict(output: EventConductionOutput) -> dict[str, Any]:
    """将 EventConductionOutput 转为可序列化 dict（供日志/测试断言）。"""
    status = output.status
    return {
        "event_id": status.event_id,
        "title": status.title,
        "success": status.success,
        "event_generated": status.event_generated,
        "persisted": status.persisted,
        "error": status.error,
        "error_type": status.error_type,
    }
