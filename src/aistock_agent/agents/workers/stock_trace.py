"""Stock Trace 受限 LLM Worker。

该 Worker 由后续 Redis Stream Consumer 调用；当前不写入 Node Artifact，避免绕过
Node 侧 Artifact 发布与用户授权边界。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
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


def _strip_post_window_market_references(
    payload: dict[str, object], snapshot: StockTraceSnapshot
) -> None:
    """Keep the LLM bounded to the frozen event window before Node writeback."""
    late_market_ids = {
        source.source_id
        for source in snapshot.source_records
        if source.kind == "market_fact"
        and source.occurred_at
        and source.occurred_at > snapshot.trigger_event.window_end_at
    }
    if not late_market_ids:
        return
    for candidate in payload.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        for field in ("supporting_evidence_ids", "counter_evidence_ids"):
            candidate[field] = [
                source_id
                for source_id in candidate.get(field, [])
                if source_id not in late_market_ids
            ]
    for chain in payload.get("chains", []):
        if not isinstance(chain, dict):
            continue
        for node in chain.get("nodes", []):
            if not isinstance(node, dict):
                continue
            for field in ("evidence_ids", "counter_evidence_ids"):
                node[field] = [
                    source_id
                    for source_id in node.get(field, [])
                    if source_id not in late_market_ids
                ]
            if node.get("epistemic_type") == "fact" and not node["evidence_ids"]:
                node["epistemic_type"] = "inference"
                node["status"] = "not_established"


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
                                " probable/insufficient）。"
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
                    _strip_post_window_market_references(payload_data, snapshot)
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
                    result = StockTraceResult.model_validate({
                        "schema_version": "stock-trace-result-v1",
                        "event_id": event_id,
                        "snapshot_id": snapshot.snapshot_id,
                        "analysis_version": analysis_version,
                        **payload_data,
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
            return StockTraceWorkerOutcome(status="failed", error_code="VALIDATION_REJECTED")
        except Exception as exc:
            logger.exception("stock_trace_worker_failed", event_id=event_id, error=str(exc))
            return StockTraceWorkerOutcome(
                status="failed", error_code="LLM_OR_DEPENDENCY_UNAVAILABLE"
            )
