"""Task 3：归因失败真实异常上报（error_detail）。

覆盖 worker / consumer / client 三处：
- worker 通用兜底异常要把"类名: 消息"截断 500 字写入 outcome.error_detail，
  且错误码仍是 LLM_OR_DEPENDENCY_UNAVAILABLE（不得误改重投语义）。
- consumer 失败路径要把 outcome.error_detail 传给 report_job。
- client。report_job 请求体条件写入 last_error_detail（None 时不写）。
"""

import pytest

from aistock_agent.agents.workers.stock_trace import (
    StockTraceWorker,
    StockTraceWorkerOutcome,
)
from aistock_agent.services.stock_trace_client import StockTraceNodeClient
from aistock_agent.workers.stock_trace_consumer import StockTraceConsumer

VALID_EVENT = "mv:000004:2026-07-30:1:up"


def snapshot_payload() -> dict[str, object]:
    """完整合法的 StockTraceSnapshot 载荷，确保 worker 走到 LLM 调用再抛异常。"""
    return {
        "snapshotId": "snapshot-001",
        "eventId": VALID_EVENT,
        "triggerRevision": 1,
        "snapshotStage": "enriched",
        "sourceRevisionHash": "a" * 64,
        "missingFields": [],
        "dataReadiness": {"company": "complete", "sector": "partial", "market": "complete"},
        "collectorVersions": {},
        "capturedAt": "2026-07-30T10:00:00+00:00",
        "triggerEvent": {
            "eventId": VALID_EVENT,
            "triggerRevision": 1,
            "symbol": "000004",
            "stockName": "Test Stock",
            "tradingDate": "2026-07-30",
            "direction": "up",
            "triggeredAt": "2026-07-30T10:00:00+00:00",
            "windowStartAt": "2026-07-30T10:00:00+00:00",
            "windowEndAt": "2026-07-30T10:00:00+00:00",
            "latestPrice": 22.0,
            "previousClose": 20.0,
            "actualValue": 10.0,
            "thresholdValue": 7.0,
            "severity": "critical",
            "ruleVersion": "price-v1",
        },
        "sourceRecords": [],
    }


class FakeSnapshotClient:
    async def get(self, path: str) -> dict[str, object] | None:
        return snapshot_payload() if "analysis-context" in path else None

    async def post(self, _p: str, _b: dict[str, object]) -> dict[str, object] | None:
        return {}

    async def patch(self, _p: str, _b: dict[str, object]) -> dict[str, object] | None:
        return {"attemptCount": 1}


class FakeLlmGenericError:
    calls = 0

    def with_structured_output(self, schema, *, method, include_raw=False):
        return self

    async def ainvoke(self, _messages):
        self.__class__.calls += 1
        raise RuntimeError("provider down")


class FakeLlmLongError:
    def with_structured_output(self, schema, *, method, include_raw=False):
        return self

    async def ainvoke(self, _messages):
        raise RuntimeError("x" * 600)


@pytest.mark.asyncio
async def test_worker_generic_exception_populates_error_detail_and_keeps_error_code() -> None:
    """通用兜底异常：error_detail 含类名+消息，错误码保持 LLM_OR_DEPENDENCY_UNAVAILABLE。"""
    FakeLlmGenericError.calls = 0
    worker = StockTraceWorker(
        StockTraceNodeClient(FakeSnapshotClient()), llm_factory=FakeLlmGenericError
    )
    outcome = await worker.analyze(VALID_EVENT, 1, "llm-stock-trace-v1")
    assert outcome.status == "failed"
    assert outcome.error_code == "LLM_OR_DEPENDENCY_UNAVAILABLE"
    assert outcome.error_detail == "RuntimeError: provider down"


@pytest.mark.asyncio
async def test_worker_error_detail_truncated_to_500_chars() -> None:
    """error_detail 截断到 500 字符（服务端同样强制截断，客户端不该传超长）。"""
    worker = StockTraceWorker(
        StockTraceNodeClient(FakeSnapshotClient()), llm_factory=FakeLlmLongError
    )
    outcome = await worker.analyze(VALID_EVENT, 1, "llm-stock-trace-v1")
    assert outcome.error_code == "LLM_OR_DEPENDENCY_UNAVAILABLE"
    assert outcome.error_detail is not None
    assert len(outcome.error_detail) <= 500
    assert outcome.error_detail == ("RuntimeError: " + "x" * 600)[:500]
    assert outcome.error_detail.startswith("RuntimeError:")


class FailureOutcomeWorker:
    async def analyze(self, _e: str, _r: int, _v: str) -> StockTraceWorkerOutcome:
        return StockTraceWorkerOutcome(
            status="failed", error_code="WORKER_FAILED",
            error_detail="RuntimeError: explode",
        )


class FakeRedisCtx:
    def __init__(self) -> None:
        self.acked: list[tuple[str, str, str]] = []

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append((stream, group, message_id))
        return 1


@pytest.mark.asyncio
async def test_consumer_failure_passes_error_detail_to_report_job() -> None:
    """失败路径：report_job 调用要带上 outcome.error_detail。"""
    recording = RecordingPatchClient()
    node_client = StockTraceNodeClient(recording)  # type: ignore[arg-type]
    consumer = StockTraceConsumer(
        FakeRedisCtx(),  # type: ignore[arg-type]
        node_client,  # type: ignore[arg-type]
        FailureOutcomeWorker(),  # type: ignore[arg-type]
    )
    await consumer._consume_message("m-1", {
        "job_id": "job-1",
        "event_id": VALID_EVENT,
        "trigger_revision": "1",
        "analysis_version": "llm-stock-trace-v1",
    })
    failure_calls = [
        body for body in recording.bodies if body.get("status") == "failed"
    ]
    assert failure_calls, "应至少有一次 status=failed 的 report_job 调用"
    assert failure_calls[0].get("last_error_detail") == "RuntimeError: explode"


class RecordingPatchClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []

    async def patch(self, path: str, body: dict[str, object]) -> dict[str, object] | None:
        del path
        self.bodies.append(body)
        return {"attemptCount": 1}


@pytest.mark.asyncio
async def test_report_job_writes_last_error_detail_when_provided() -> None:
    fake = RecordingPatchClient()
    client = StockTraceNodeClient(fake)  # type: ignore[arg-type]
    await client.report_job("job-1", "dead_letter", error_code="WORKER_FAILED",
                            error_detail="RuntimeError: boom")
    body = fake.bodies[-1]
    assert body["last_error_detail"] == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_report_job_omits_last_error_detail_when_none() -> None:
    fake = RecordingPatchClient()
    client = StockTraceNodeClient(fake)  # type: ignore[arg-type]
    await client.report_job("job-1", "failed", error_code="WORKER_FAILED")
    body = fake.bodies[-1]
    assert "last_error_detail" not in body


@pytest.mark.asyncio
async def test_report_job_truncates_error_detail_to_500_chars() -> None:
    fake = RecordingPatchClient()
    client = StockTraceNodeClient(fake)  # type: ignore[arg-type]
    await client.report_job("job-1", "dead_letter", error_code="WORKER_FAILED",
                            error_detail="RuntimeError: " + "y" * 600)
    body = fake.bodies[-1]
    assert body["last_error_detail"] == ("RuntimeError: " + "y" * 600)[:500]
    assert body["last_error_detail"].endswith("y" * 486)