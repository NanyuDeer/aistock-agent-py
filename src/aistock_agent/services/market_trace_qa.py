"""市场复盘问答服务，只消费已持久化且通过校验的 review 工件。"""

import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, model_validator

from aistock_agent.agents.workers.review import validate_trace_against_snapshot
from aistock_agent.prompts.workers.market_trace_qa import MARKET_TRACE_QA_PROMPT
from aistock_agent.schemas.market_trace import (
    CandidateExplanation,
    MarketTraceResult,
    MarketTraceSnapshot,
    SourceRecord,
)
from aistock_agent.schemas.market_trace_qa import (
    MarketTraceQaResponse,
    MarketTraceQaSource,
    MarketTraceQaTrace,
    parse_market_trace_report_date,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think

logger = structlog.get_logger()

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class _MarketTraceQaSelection(BaseModel):
    """LLM 只能选择已验证工件的回答类别和候选，不能提供自由答案。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    answer_type: Literal[
        "candidate", "dominant_phenomenon", "unresolved_questions", "out_of_scope"
    ]
    candidate_id: StrictStr | None
    source_ids: list[StrictStr]

    @model_validator(mode="after")
    def _validate_shape(self) -> "_MarketTraceQaSelection":
        if self.answer_type == "candidate" and not self.candidate_id:
            raise ValueError("candidate 回答必须指定 candidate_id")
        if self.answer_type != "candidate" and self.candidate_id is not None:
            raise ValueError("非 candidate 回答不能指定 candidate_id")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("source_ids 不能重复")
        return self


def _degraded_response(session_id: str, artifact_id: str, reason: str) -> MarketTraceQaResponse:
    """构造降级响应，不补抓实时数据，也不暴露未经完全验证的 as_of。"""
    return MarketTraceQaResponse(
        content="暂时无法回答此问题，请稍后重试。",
        session_id=session_id,
        trace=MarketTraceQaTrace(
            artifact_id=artifact_id,
            sources=[],
            as_of="",
            confidence="low",
            uncertainty=[],
            degraded=True,
            degraded_reason=reason,
        ),
    )


def _build_sources_summary(
    sources: dict[str, SourceRecord], referenced_ids: list[str]
) -> list[MarketTraceQaSource]:
    """从经过键值一致性校验的 sources map 提取来源摘要。"""
    return [
        MarketTraceQaSource(
            source_id=source_id,
            title=sources[source_id].title,
            kind=sources[source_id].kind,
            provider=sources[source_id].provider,
        )
        for source_id in referenced_ids
    ]


def _candidate_source_ids(candidate: CandidateExplanation) -> set[str]:
    source_ids = set(candidate.supporting_evidence_ids) | set(candidate.counter_evidence_ids)
    if candidate.chain:
        for node in candidate.chain.nodes:
            source_ids.update(node.evidence_ids)
    return source_ids


def _render_selection(
    selection: _MarketTraceQaSelection,
    snapshot: MarketTraceSnapshot,
    trace: MarketTraceResult,
) -> tuple[str, set[str]]:
    """将已验证的选择确定性渲染为内容，并给出可引用的来源集合。"""
    if selection.answer_type == "out_of_scope":
        return "当前复盘数据中未涵盖此问题。", set()

    if selection.answer_type == "unresolved_questions":
        if not trace.unresolved_questions:
            raise ValueError("工件没有未解问题")
        return "未解问题：" + "；".join(trace.unresolved_questions), set()

    if selection.answer_type == "dominant_phenomenon":
        phenomenon = snapshot.dominant_phenomenon
        if phenomenon is None:
            raise ValueError("工件没有主导现象")
        return f"主导现象：{phenomenon.summary}", set(phenomenon.fact_ids)

    candidates = [
        candidate for candidate in trace.candidates if candidate.id == selection.candidate_id
    ]
    if len(candidates) != 1:
        raise ValueError("candidate_id 不在已验证工件中或不唯一")

    candidate = candidates[0]
    status_label = {
        "supported": "已支持",
        "weak": "弱证据",
        "rejected": "已否定",
        "insufficient": "证据不足",
    }[candidate.status]
    content = (
        f"复盘候选（{status_label}）：{candidate.verdict}。"
        "这是已归档复盘中的证据归因，不等同于确认因果关系。"
    )
    return content, _candidate_source_ids(candidate)


def _parse_selection(raw_text: str) -> _MarketTraceQaSelection:
    """解析严格 JSON 选择契约，顶层非对象、类型或额外字段均拒绝。"""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.split("\n") if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("模型输出顶层必须是对象")
    return _MarketTraceQaSelection.model_validate(payload)


async def answer_market_trace_qa(
    message: str,
    report_date: str | None,
    session_id: str | None,
) -> MarketTraceQaResponse:
    """在不重跑 Trace、不取实时行情的前提下回答市场复盘问题。"""
    session = session_id or f"mtqa_{datetime.now(_SHANGHAI_TZ).strftime('%Y%m%d%H%M%S')}"
    artifact_id = ""

    try:
        report_day = (
            parse_market_trace_report_date(report_date)
            if report_date is not None
            else datetime.now(_SHANGHAI_TZ).date()
        )
    except (TypeError, ValueError):
        return _degraded_response(session, artifact_id, "报告日期非法")
    trade_date = report_day.isoformat()

    try:
        report_result = await node_api.get_review_analysis_report(report_day)
    except Exception:
        logger.exception("market_trace_qa_fetch_report_failed", report_date=trade_date)
        return _degraded_response(session, artifact_id, "报告服务读取失败/暂不可用")

    if report_result.status == "not_found":
        return _degraded_response(session, artifact_id, f"当日（{trade_date}）无市场复盘报告")
    if report_result.status != "found" or not isinstance(report_result.report, dict):
        return _degraded_response(session, artifact_id, "报告服务读取失败/暂不可用")
    report = report_result.report
    if report.get("status") != "completed":
        return _degraded_response(session, artifact_id, "复盘报告未完成")

    content = report.get("content")
    if not isinstance(content, dict):
        return _degraded_response(session, artifact_id, "复盘报告格式非法")
    market_trace = content.get("market_trace")
    if not isinstance(market_trace, dict):
        return _degraded_response(session, artifact_id, "复盘报告缺少 market_trace 字段")
    snapshot_raw = market_trace.get("snapshot")
    trace_raw = market_trace.get("trace")
    if not isinstance(snapshot_raw, dict) or not isinstance(trace_raw, dict):
        return _degraded_response(session, artifact_id, "复盘报告缺少 snapshot/trace 数据")

    try:
        snapshot = MarketTraceSnapshot.model_validate(snapshot_raw)
        trace = MarketTraceResult.model_validate(trace_raw)
    except ValidationError:
        logger.exception("market_trace_qa_parse_failed", report_date=trade_date)
        return _degraded_response(session, artifact_id, "复盘报告数据解析失败")

    if any(source_id != record.source_id for source_id, record in snapshot.sources.items()):
        return _degraded_response(session, artifact_id, "复盘报告来源映射不一致")
    try:
        validate_trace_against_snapshot(trace, snapshot)
    except ValueError:
        logger.exception("market_trace_qa_validation_failed", report_date=trade_date)
        return _degraded_response(session, artifact_id, "复盘报告校验失败")
    if snapshot.trade_date != trade_date:
        logger.warning(
            "market_trace_qa_date_mismatch",
            snapshot_date=snapshot.trade_date,
            report_date=trade_date,
        )
        return _degraded_response(session, artifact_id, "复盘报告日期不匹配")

    report_id = report.get("id")
    artifact_id = (
        report_id.strip()
        if isinstance(report_id, str) and report_id.strip()
        else snapshot.snapshot_id
    )

    snapshot_json = snapshot.model_dump_json(indent=2)
    trace_json = trace.model_dump_json(indent=2)
    try:
        llm = get_deep_think()
        result = await llm.ainvoke(
            [
                SystemMessage(content=MARKET_TRACE_QA_PROMPT),
                HumanMessage(
                    content=(
                        f"用户问题：\n{message}\n\n"
                        f"已冻结的 MarketTraceSnapshot：\n{snapshot_json}\n\n"
                        f"已验证的 MarketTraceResult：\n{trace_json}"
                    )
                ),
            ]
        )
    except Exception:
        logger.exception("market_trace_qa_llm_failed", report_date=trade_date)
        return _degraded_response(session, artifact_id, "模型调用失败")

    raw_text = result.content if isinstance(result.content, str) else ""
    try:
        selection = _parse_selection(raw_text)
        response_content, allowed_source_ids = _render_selection(selection, snapshot, trace)
    except (json.JSONDecodeError, ValidationError, ValueError):
        logger.warning("market_trace_qa_llm_selection_invalid", output=raw_text[:200])
        return _degraded_response(session, artifact_id, "模型选择格式非法或范围不合法")

    source_ids = selection.source_ids
    if not set(source_ids).issubset(allowed_source_ids):
        return _degraded_response(session, artifact_id, "模型选择了不匹配的来源")
    if not set(source_ids).issubset(snapshot.sources):
        return _degraded_response(session, artifact_id, "模型选择了未知来源")

    return MarketTraceQaResponse(
        content=response_content,
        session_id=session,
        trace=MarketTraceQaTrace(
            artifact_id=artifact_id,
            sources=_build_sources_summary(snapshot.sources, source_ids),
            as_of=snapshot.captured_at.isoformat(),
            confidence=trace.confidence,
            uncertainty=trace.unresolved_questions,
            degraded=False,
            degraded_reason=None,
        ),
    )
