import asyncio
import time
from datetime import UTC, date, datetime

import pytest

from aistock_agent.agents.workers.stock_trace import (
    StockTraceWorker,
    StockTraceWorkerOutcome,
    _recover_tool_payload,
)
from aistock_agent.schemas.stock_trace import (
    StockTraceResult,
    StockTraceResultPayload,
    StockTraceTriggerRequest,
    StockTraceTriggerResponse,
)
from aistock_agent.services.stock_trace_client import StockTraceNodeClient
from aistock_agent.services.stock_trace_validator import (
    StockTraceValidationError,
    validate_stock_trace_result,
)
from aistock_agent.workers.stock_trace_consumer import StockTraceConsumer

NOW = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)


def snapshot_payload() -> dict[str, object]:
    return {
        "snapshotId": "snapshot-001",
        "eventId": "mv:000004:2026-07-30:1:up",
        "triggerRevision": 1,
        "snapshotStage": "enriched",
        "sourceRevisionHash": "a" * 64,
        "triggerEvent": {
            "eventId": "mv:000004:2026-07-30:1:up",
            "triggerRevision": 1,
            "symbol": "000004",
            "stockName": "Test Stock",
            "tradingDate": "2026-07-30",
            "direction": "up",
            "triggeredAt": NOW.isoformat(),
            "windowStartAt": NOW.isoformat(),
            "windowEndAt": NOW.isoformat(),
            "latestPrice": 22.0,
            "previousClose": 20.0,
            "actualValue": 10.0,
            "thresholdValue": 7.0,
            "severity": "critical",
            "ruleVersion": "price-v1",
        },
        "missingFields": [],
        "dataReadiness": {"company": "complete", "sector": "partial", "market": "complete"},
        "collectorVersions": {},
        "capturedAt": NOW.isoformat(),
        "sourceRecords": [
            {
                "sourceId": "announcement-1",
                "kind": "announcement",
                "provider": "test_exchange",
                "sourceLevel": "A",
                "title": "Material restructuring plan",
                "contentExcerpt": "Official disclosure",
                "occurredAt": NOW.isoformat(),
                "capturedAt": NOW.isoformat(),
                "payload": {"impact": "positive"},
                "contentHash": "b" * 64,
            },
            {
                "sourceId": "trigger-1",
                "kind": "trigger_fact",
                "provider": "detector",
                "sourceLevel": "A",
                "title": "Price trigger",
                "contentExcerpt": "Price up 10%",
                "occurredAt": NOW.isoformat(),
                "capturedAt": NOW.isoformat(),
                "payload": {},
                "contentHash": "c" * 64,
            },
        ],
    }


class FakeNodeClient:
    async def get(self, path: str) -> dict[str, object] | None:
        return snapshot_payload() if "analysis-context" in path else None

    async def post(self, _path: str, _body: dict[str, object]) -> dict[str, object] | None:
        return {}

    async def patch(self, _path: str, _body: dict[str, object]) -> dict[str, object] | None:
        return {"attemptCount": 1}


def valid_result() -> StockTraceResult:
    stages = [
        "structural_root", "trigger", "transmission", "exposure", "repricing", "observable_result"
    ]
    nodes = [
        {
            "node_id": f"node-{index}",
            "stage": stage,
            "stage_order": index,
            "epistemic_type": (
                "fact"
                if stage in {"structural_root", "trigger", "observable_result"}
                else "inference"
            ),
            "status": (
                "established"
                if stage in {"structural_root", "trigger", "observable_result"}
                else "partial"
            ),
            "claim": f"{stage} claim",
            "evidence_ids": (
                ["trigger-1"]
                if stage in {"trigger", "observable_result"}
                else ["announcement-1"]
            ),
            "counter_evidence_ids": [],
        }
        for index, stage in enumerate(stages, start=1)
    ]
    return StockTraceResult.model_validate({
        "schema_version": "stock-trace-result-v1",
        "event_id": "mv:000004:2026-07-30:1:up",
        "snapshot_id": "snapshot-001",
        "analysis_version": "llm-stock-trace-v1",
        "attribution_status": "confirmed",
        "primary_phrase": "大额订单利好",
        "primary_chain_id": "chain-1",
        "confidence_score": 0.8,
        "confidence_level": "high",
        "candidates": [{
            "candidate_id": "candidate-1", "layer": "company", "rank": 1,
            "status": "supported", "verdict": "Official disclosure supports the cause.",
            "supporting_evidence_ids": ["announcement-1"], "counter_evidence_ids": [],
        }, {
            "candidate_id": "candidate-2", "layer": "sector", "rank": 1,
            "status": "insufficient", "verdict": "No sector explanation is established.",
            "supporting_evidence_ids": [], "counter_evidence_ids": [],
        }, {
            "candidate_id": "candidate-3", "layer": "market", "rank": 1,
            "status": "insufficient", "verdict": "No market explanation is established.",
            "supporting_evidence_ids": [], "counter_evidence_ids": [],
        }, {
            "candidate_id": "candidate-4", "layer": "capital", "rank": 1,
            "status": "insufficient", "verdict": "No capital explanation is established.",
            "supporting_evidence_ids": [], "counter_evidence_ids": [],
        }, {
            "candidate_id": "candidate-5", "layer": "technical", "rank": 1,
            "status": "insufficient", "verdict": "No technical explanation is established.",
            "supporting_evidence_ids": [], "counter_evidence_ids": [],
        }],
        "chains": [{
            "chain_id": "chain-1",
            "candidate_id": "candidate-1",
            "role": "primary",
            "nodes": nodes,
        }],
        "suggested_actions": ["verify_announcement", "observe"],
    })


def test_node_client_normalizes_node_camel_case_analysis_context() -> None:
    client = StockTraceNodeClient(FakeNodeClient())
    snapshot = asyncio.run(client.get_analysis_context("mv:000004:2026-07-30:1:up", 1))
    assert snapshot is not None
    assert snapshot.trigger_event.event_id == snapshot.event_id
    assert snapshot.source_records[0].source_level == "A"


def test_validator_accepts_a_level_confirmed_company_cause() -> None:
    snapshot = asyncio.run(
        StockTraceNodeClient(FakeNodeClient()).get_analysis_context("mv:000004:2026-07-30:1:up", 1)
    )
    assert snapshot is not None
    validate_stock_trace_result(valid_result(), snapshot)


def test_validator_rejects_unknown_evidence() -> None:
    snapshot = asyncio.run(
        StockTraceNodeClient(FakeNodeClient()).get_analysis_context("mv:000004:2026-07-30:1:up", 1)
    )
    assert snapshot is not None
    result = valid_result().model_copy(deep=True)
    result.candidates[0].supporting_evidence_ids = ["missing"]
    with pytest.raises(StockTraceValidationError, match="unknown source"):
        validate_stock_trace_result(result, snapshot)


class FakeLlm:
    structured_schema: type[object] | None = None
    structured_method: str | None = None

    def with_structured_output(
        self, schema: type[object], *, method: str, include_raw: bool = False
    ) -> "FakeLlm":
        self.__class__.structured_schema = schema
        self.__class__.structured_method = method
        return self

    async def ainvoke(self, _messages: list[object]) -> StockTraceResultPayload:
        return StockTraceResultPayload.model_validate(
            valid_result().model_dump(
                exclude={"schema_version", "event_id", "snapshot_id", "analysis_version"}
            )
        )


@pytest.mark.asyncio
async def test_worker_returns_validated_structured_result() -> None:
    worker = StockTraceWorker(StockTraceNodeClient(FakeNodeClient()), llm_factory=FakeLlm)
    outcome = await worker.analyze("mv:000004:2026-07-30:1:up", 1, "llm-stock-trace-v1")
    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.attribution_status == "confirmed"
    assert FakeLlm.structured_schema is StockTraceResultPayload
    assert FakeLlm.structured_method == "function_calling"


class FakeLlmFailsFirst:
    calls = 0
    structured_schema: type[object] | None = None

    def with_structured_output(
        self, schema: type[object], *, method: str, include_raw: bool = False
    ) -> "FakeLlmFailsFirst":
        self.__class__.structured_schema = schema
        return self

    async def ainvoke(self, _messages: list[object]) -> StockTraceResultPayload:
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            raise StockTraceValidationError("fact node requires evidence")
        valid = valid_result().model_dump(
            exclude={
                "schema_version",
                "event_id",
                "snapshot_id",
                "analysis_version",
            }
        )
        return StockTraceResultPayload.model_validate(valid)


class FakeLlmAlwaysFails:
    calls = 0

    def with_structured_output(
        self, schema: type[object], *, method: str, include_raw: bool = False
    ) -> "FakeLlmAlwaysFails":
        return self

    async def ainvoke(self, _messages: list[object]) -> StockTraceResultPayload:
        self.__class__.calls += 1
        raise StockTraceValidationError("confirmed requires high confidence")


class FakeLlmGenericError:
    calls = 0

    def with_structured_output(
        self, schema: type[object], *, method: str, include_raw: bool = False
    ) -> "FakeLlmGenericError":
        return self

    async def ainvoke(self, _messages: list[object]) -> StockTraceResultPayload:
        self.__class__.calls += 1
        raise RuntimeError("provider down")


@pytest.mark.asyncio
async def test_worker_retries_once_on_validation_error_then_succeeds() -> None:
    FakeLlmFailsFirst.calls = 0
    worker = StockTraceWorker(StockTraceNodeClient(FakeNodeClient()), llm_factory=FakeLlmFailsFirst)
    outcome = await worker.analyze("mv:000004:2026-07-30:1:up", 1, "llm-stock-trace-v1")
    assert outcome.status == "completed"
    assert outcome.result is not None
    assert FakeLlmFailsFirst.calls == 2, "校验失败应带纠错提示重试一次"


@pytest.mark.asyncio
async def test_worker_retries_once_then_fails_with_validation_rejected() -> None:
    FakeLlmAlwaysFails.calls = 0
    worker = StockTraceWorker(
        StockTraceNodeClient(FakeNodeClient()), llm_factory=FakeLlmAlwaysFails
    )
    outcome = await worker.analyze("mv:000004:2026-07-30:1:up", 1, "llm-stock-trace-v1")
    assert outcome.status == "failed"
    assert outcome.error_code == "VALIDATION_REJECTED"
    assert FakeLlmAlwaysFails.calls == 2, "最多重试一次"


@pytest.mark.asyncio
async def test_worker_does_not_retry_generic_llm_error() -> None:
    FakeLlmGenericError.calls = 0
    worker = StockTraceWorker(
        StockTraceNodeClient(FakeNodeClient()), llm_factory=FakeLlmGenericError
    )
    outcome = await worker.analyze("mv:000004:2026-07-30:1:up", 1, "llm-stock-trace-v1")
    assert outcome.status == "failed"
    assert outcome.error_code == "LLM_OR_DEPENDENCY_UNAVAILABLE"
    assert FakeLlmGenericError.calls == 1, "非校验异常不重试"


def test_recovers_single_trailing_brace_in_provider_tool_arguments() -> None:
    arguments = valid_result().model_dump_json(
        exclude={"schema_version", "event_id", "snapshot_id", "analysis_version"}
    ) + "}"

    class RawResponse:
        additional_kwargs = {"tool_calls": [{"function": {"arguments": arguments}}]}

    payload = _recover_tool_payload(RawResponse())
    assert payload["attribution_status"] == "confirmed"


class FakeRedis:
    def __init__(self) -> None:
        self.acked: list[tuple[str, str, str]] = []
        self.store: dict[str, str] = {}
        self.added: list[tuple[str, dict[bytes, bytes]]] = []

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append((stream, group, message_id))
        return 1

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        del ttl
        self.store[key] = value

    async def delete(self, *keys: str) -> int:
        removed = sum(1 for k in keys if k in self.store)
        for k in keys:
            self.store.pop(k, None)
        return removed

    async def xadd(self, stream: str, payload: dict[bytes, bytes]) -> str:
        # 模拟真实 redis.asyncio.xadd：str key/value 按协议编码为 bytes。
        encoded = {
            (k if isinstance(k, bytes) else str(k).encode()): (
                str(v).encode()
            )
            for k, v in payload.items()
        }
        self.added.append((stream, encoded))
        return "dlq-0"


class CompletedWorker:
    async def analyze(
        self, _event_id: str, _trigger_revision: int, _analysis_version: str
    ) -> StockTraceWorkerOutcome:
        return StockTraceWorkerOutcome(status="completed", result=valid_result())


@pytest.mark.asyncio
async def test_consumer_writes_validated_result_then_acknowledges_job() -> None:
    redis_client = FakeRedis()
    consumer = StockTraceConsumer(
        redis_client,  # type: ignore[arg-type]
        StockTraceNodeClient(FakeNodeClient()),
        CompletedWorker(),  # type: ignore[arg-type]
    )
    await consumer._consume_message("1710000000000-0", {
        "job_id": "job-001",
        "event_id": "mv:000004:2026-07-30:1:up",
        "trigger_revision": "1",
        "analysis_version": "llm-stock-trace-v1",
    })
    assert redis_client.acked == [
        ("stock-trace.jobs", "stock-trace-workers", "1710000000000-0")
    ]


class SnapshotNotReadyWorker:
    async def analyze(self, _e: str, _r: int, _v: str) -> StockTraceWorkerOutcome:
        return StockTraceWorkerOutcome(status="failed", error_code="SNAPSHOT_NOT_READY")


@pytest.mark.asyncio
async def test_consumer_snapshot_not_ready_first_seen_does_not_dead_letter() -> None:
    redis_client = FakeRedis()
    consumer = StockTraceConsumer(
        redis_client,  # type: ignore[arg-type]
        StockTraceNodeClient(FakeNodeClient()),
        SnapshotNotReadyWorker(),  # type: ignore[arg-type]
    )
    # 首次命中：标记 first_seen，不 dead-letter，不 ack
    await consumer._consume_message("m-1", {
        "job_id": "job-1",
        "event_id": "mv:000004:2026-07-30:1:up",
        "trigger_revision": "1",
        "analysis_version": "llm-stock-trace-v1",
    })
    assert redis_client.acked == []
    assert redis_client.store["stock_trace:snapshot_pending:job-1"]


@pytest.mark.asyncio
async def test_consumer_snapshot_not_ready_timeout_dead_letters() -> None:
    redis_client = FakeRedis()
    # 模拟 first_seen 已超过阈值（10 分钟前）
    redis_client.store["stock_trace:snapshot_pending:job-1"] = str(int(time.time()) - 601)
    consumer = StockTraceConsumer(
        redis_client,  # type: ignore[arg-type]
        StockTraceNodeClient(FakeNodeClient()),
        SnapshotNotReadyWorker(),  # type: ignore[arg-type]
    )
    await consumer._consume_message("m-1", {
        "job_id": "job-1",
        "event_id": "mv:000004:2026-07-30:1:up",
        "trigger_revision": "1",
        "analysis_version": "llm-stock-trace-v1",
    })
    assert redis_client.added, "超时应进 DLQ"
    assert redis_client.added[0][0] == "stock-trace.jobs.dlq"
    assert redis_client.added[0][1][b"error_code"] == b"SNAPSHOT_TIMEOUT"


def test_trigger_request_validates_symbol_and_optional_fields() -> None:
    request = StockTraceTriggerRequest(
        symbol="000001",
        cycle="short",
        report_date="2026-07-30",
        trace_id="trace-001",
    )
    assert request.symbol == "000001"
    assert request.cycle == "short"
    assert request.report_date == date(2026, 7, 30)
    assert request.trace_id == "trace-001"


def test_trigger_response_supports_completed_and_degraded() -> None:
    completed = StockTraceTriggerResponse(
        trace_id="trace-001",
        symbol="000001",
        report_date="2026-07-30",
        status="completed",
        report_id=42,
    )
    degraded = StockTraceTriggerResponse(
        trace_id="trace-002",
        symbol="600519",
        report_date="2026-07-30",
        status="degraded",
        degraded_reason="LLM temporarily unavailable",
    )
    assert completed.report_id == 42
    assert degraded.degraded_reason == "LLM temporarily unavailable"
