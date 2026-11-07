"""Stock Trace 受限 LLM Worker。

该 Worker 由后续 Redis Stream Consumer 调用；当前不写入 Node Artifact，避免绕过
Node 侧 Artifact 发布与用户授权边界。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Protocol, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from aistock_agent.config import settings
from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.workers.stock_trace import STOCK_TRACE_PROMPT
from aistock_agent.schemas.stock_trace import (
    StockTraceResult,
    StockTraceResultPayload,
    StockTraceSnapshot,
)
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.stock_trace_client import StockTraceNodeClient
from aistock_agent.services.stock_trace_validator import (
    StockTraceValidationError,
    validate_stock_trace_result,
)

logger = structlog.get_logger()

_metrics = get_metrics_collector()


class StockTraceLlm(Protocol):
    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        method: str,
        include_raw: bool = False,
    ) -> "StructuredStockTraceLlm": ...


class StructuredStockTraceLlm(Protocol):
    async def ainvoke(
        self, messages: list[object]
    ) -> BaseModel | dict[str, object]: ...


@dataclass(frozen=True)
class StockTraceWorkerOutcome:
    status: str
    result: StockTraceResult | None = None
    error_code: str | None = None
    error_detail: str | None = None


def _first_json_object(value: str) -> str:
    """Return one complete JSON object and accept exactly one accidental trailing brace."""
    depth = 0
    in_string = False
    escaped = False
    start: int | None = None
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{" and start is None:
            start = index
            depth = 1
        elif char == "{" and start is not None:
            depth += 1
        elif char == "}" and start is not None:
            depth -= 1
            if depth == 0:
                trailing = value[index + 1 :].strip()
                if trailing not in {"", "}"}:
                    raise ValueError(
                        "structured tool arguments contain non-recoverable trailing data"
                    )
                return value[start : index + 1]
    raise ValueError("structured tool arguments do not contain a complete JSON object")


def _recover_tool_payload(raw_response: object) -> dict[str, object]:
    """Recover only the known provider defect: a single trailing `}` in tool arguments."""
    additional_kwargs = getattr(raw_response, "additional_kwargs", {})
    if not isinstance(additional_kwargs, dict):
        raise ValueError("structured output parser did not expose raw tool arguments")
    tool_calls = additional_kwargs.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls or not isinstance(tool_calls[0], dict):
        raise ValueError("structured output parser did not expose a tool call")
    function = tool_calls[0].get("function")
    arguments = function.get("arguments") if isinstance(function, dict) else None
    if not isinstance(arguments, str):
        raise ValueError("structured output parser did not expose string tool arguments")
    value = json.loads(_first_json_object(arguments))
    if not isinstance(value, dict):
        raise ValueError("structured tool arguments must decode to an object")
    return cast(dict[str, object], value)


_SOURCE_DOMAINS = {
    "news": "company",
    "announcement": "company",
    "sector_fact": "sector",
    "sector_news": "sector",
    "market_fact": "market",
    "capital_fact": "capital",
    "technical_fact": "technical",
    "insight_article": "article",
}
_CHINA_TIME = timezone(timedelta(hours=8))


def _snapshot_at_trigger(snapshot: StockTraceSnapshot) -> StockTraceSnapshot:
    """Use the revision's trigger time, even when enrichment runs much later."""
    cutoff = snapshot.trigger_event.triggered_at
    sources = [
        source for source in snapshot.source_records
        if (source.occurred_at or source.captured_at) <= cutoff
        and (
            source.kind in {"trigger_fact", "quote_fact"}
            or source.window_end is None
            or source.window_end <= cutoff
        )
    ]
    if len(sources) == len(snapshot.source_records):
        return snapshot

    retained_ids = {source.source_id for source in sources}
    available = {_SOURCE_DOMAINS.get(source.kind) for source in sources}
    removed = {
        _SOURCE_DOMAINS[source.kind]
        for source in snapshot.source_records
        if source.source_id not in retained_ids and source.kind in _SOURCE_DOMAINS
    }
    readiness = dict(snapshot.data_readiness)
    for domain in removed:
        readiness[domain] = "partial" if domain in available else "missing"
    missing = list(dict.fromkeys([
        *snapshot.missing_fields, *(f"{domain}_context" for domain in sorted(removed)),
    ]))
    return snapshot.model_copy(update={
        "source_records": sources,
        "data_readiness": readiness,
        "missing_fields": missing,
    })


def _downgrade_unsupported_primary(
    payload: StockTraceResultPayload, snapshot: StockTraceSnapshot, primary_id: str | None,
) -> StockTraceResultPayload:
    primary = next((chain for chain in payload.chains if chain.chain_id == primary_id), None)
    candidate = next(
        (item for item in payload.candidates if primary and item.candidate_id == primary.candidate_id),
        None,
    )
    support_ids = set(candidate.supporting_evidence_ids) if candidate else set()
    trade_date = snapshot.trigger_event.trading_date
    has_causal_support = any(
        source.source_id in support_ids
        and source.kind not in {"trigger_fact", "quote_fact"}
        and not (
            source.kind in {"capital_fact", "sector_fact"}
            and (
                (source.occurred_at or source.captured_at)
                .astimezone(_CHINA_TIME).date().isoformat() < trade_date
            )
        )
        for source in snapshot.source_records
    )
    if payload.attribution_status not in {"confirmed", "hypothesis"} or has_causal_support:
        return payload

    return payload.model_copy(update={
        "attribution_status": "insufficient",
        "primary_phrase": "证据不足",
        "confidence_score": min(payload.confidence_score, 0.30),
        "confidence_level": "low",
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "chains": [],
        "candidates": [
            item.model_copy(update={
                "status": "insufficient",
                "verdict": "缺少支持触发归因的有效证据",
                "supporting_evidence_ids": [],
            }) if item is candidate else item
            for item in payload.candidates
        ],
    })


def _default_llm_factory() -> StockTraceLlm:
    """适配 LangChain 宽输入签名到 Worker 所需的最小协议。"""
    base_url = (settings.deep_think_base_url or settings.openai_base_url).lower()
    # DeepSeek V4 defaults to Thinking Mode, which rejects an enforced
    # tool_choice. Stock Trace has exactly one schema-output tool and does not
    # need a multi-turn reasoning/tool loop, so disable that mode for this call.
    extra_body = {"thinking": {"type": "disabled"}} if "deepseek" in base_url else None
    return cast(StockTraceLlm, get_deep_think(extra_body=extra_body))


class StockTraceWorker:
    def __init__(
        self,
        client: StockTraceNodeClient | None = None,
        llm_factory: Callable[[], StockTraceLlm] = _default_llm_factory,
    ) -> None:
        self._client = client or StockTraceNodeClient()
        self._llm_factory = llm_factory

    async def analyze(
        self, event_id: str, trigger_revision: int, analysis_version: str
    ) -> StockTraceWorkerOutcome:
        """仅按 event_id 获取冻结上下文，并执行最多一次受限 LLM 调用。"""
        try:
            snapshot = await self._client.get_analysis_context(event_id, trigger_revision)
            if snapshot is None or snapshot.event_id != event_id:
                return StockTraceWorkerOutcome(status="failed", error_code="SNAPSHOT_NOT_READY")
            snapshot = _snapshot_at_trigger(snapshot)
            llm = self._llm_factory()
            structured_llm = llm.with_structured_output(
                StockTraceResultPayload,
                method=settings.stock_trace_structured_output_method,
                include_raw=True,
            )
            last_error: str | None = None
            for attempt in range(2):  # 首次 + 校验失败纠错一次（token 保护，不无限重试）
                try:
                    messages: list[object] = [
                        SystemMessage(content=STOCK_TRACE_PROMPT),
                        HumanMessage(content=snapshot.model_dump_json()),
                    ]
                    if last_error is not None:
                        messages.append(HumanMessage(
                            content=(
                                "你上一轮输出未通过确定性校验，请修正后重试。"
                                f"校验错误详情：{last_error}\n"
                                "请对照上面错误逐项修正（缺失字段补齐、未引用的 source_id 删除、"
                                "选中链补全六阶段、confirmed 条件不满足时降级为"
                                " hypothesis/insufficient）。"
                            )
                        ))
                    response = await structured_llm.ainvoke(messages)
                    if isinstance(response, dict) and "parsed" in response:
                        parsed = response.get("parsed")
                        raw_payload = (
                            parsed.model_dump(mode="python")
                            if isinstance(parsed, BaseModel)
                            else parsed
                        )
                        if raw_payload is None:
                            raw_payload = _recover_tool_payload(response.get("raw"))
                    else:
                        raw_payload = (
                            response.model_dump(mode="python")
                            if isinstance(response, BaseModel)
                            else response
                        )
                    payload = StockTraceResultPayload.model_validate(raw_payload)
                    payload_data = payload.model_dump(mode="python")
                    chain_ids = {chain["chain_id"] for chain in payload_data["chains"]}
                    primary_chain_id = payload.primary_chain_id
                    alternative_chain_id = payload.alternative_chain_id
                    if primary_chain_id not in chain_ids:
                        primary_chain_id = next(
                            (
                                chain["chain_id"]
                                for chain in payload_data["chains"]
                                if chain["role"] == "primary"
                            ),
                            next(iter(chain_ids), None),
                        )
                    if alternative_chain_id not in chain_ids:
                        alternative_chain_id = next(
                            (
                                chain["chain_id"]
                                for chain in payload_data["chains"]
                                if chain["role"] == "alternative"
                            ),
                            None,
                        )
                    payload_data["primary_chain_id"] = primary_chain_id
                    payload_data["alternative_chain_id"] = alternative_chain_id
                    # Chain role is redundant metadata: its only valid value is defined
                    # by the selected chain ids. Derive it here instead of letting an LLM
                    # create a conflicting second identity for the same chain.
                    for chain in payload_data["chains"]:
                        if chain["chain_id"] == primary_chain_id:
                            chain["role"] = "primary"
                        elif chain["chain_id"] == alternative_chain_id:
                            chain["role"] = "alternative"
                    payload = _downgrade_unsupported_primary(
                        StockTraceResultPayload.model_validate(payload_data),
                        snapshot,
                        primary_chain_id,
                    )
                    result = StockTraceResult.model_validate({
                        "schema_version": "stock-trace-result-v1",
                        "event_id": event_id,
                        "snapshot_id": snapshot.snapshot_id,
                        "analysis_version": analysis_version,
                        **payload.model_dump(mode="python"),
                    })
                    validate_stock_trace_result(result, snapshot)
                    if attempt == 1:
                        _metrics.record_stock_trace_validation_retry_success()
                    return StockTraceWorkerOutcome(status="completed", result=result)
                except (ValueError, StockTraceValidationError) as exc:
                    # 解析/校验类失败：记录 + 纠错重试一次（第二次循环直接落到返回值）
                    _metrics.record_stock_trace_validation_failed(type(exc).__name__)
                    logger.warning(
                        "stock_trace_validation_failed",
                        event_id=event_id, attempt=attempt + 1, error=str(exc),
                    )
                    last_error = str(exc)
                    continue
            # 校验失败（高频）也带上最后一条失败原因，供失败可查；last_error
            # 只在循环内校验异常时赋值，此处做 None 容忍以防边界未覆盖。
            return StockTraceWorkerOutcome(
                status="failed",
                error_code="VALIDATION_REJECTED",
                error_detail=(last_error[:500] if last_error else None),
            )
        except Exception as exc:
            logger.exception("stock_trace_worker_failed", event_id=event_id, error=str(exc))
            # 兜底只落笼统码会丢失真实根因（2026-09-30 事故教训），把"类名: 消息"
            # 截断 500 字随 outcome 上报；错误码不动以维持重投语义。
            detail = f"{type(exc).__name__}: {exc}"
            return StockTraceWorkerOutcome(
                status="failed",
                error_code="LLM_OR_DEPENDENCY_UNAVAILABLE",
                error_detail=detail[:500],
            )
