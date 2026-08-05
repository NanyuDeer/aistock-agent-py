"""insight_consumer 骨架测试 — mock redis + mock worker 验证 ack / dead_letter / 重试。"""

import pytest

from aistock_agent.workers.insight_consumer import (
    DLQ_STREAM,
    STREAM,
    InsightConsumer,
    InsightWorkerOutcome,
)

GROUP = "watchlist-insight-workers"


class FakeRedis:
    def __init__(
        self,
        *,
        claimed_entries: list[tuple[str, dict[str, str]]] | None = None,
        new_entries: list[tuple[str, dict[str, str]]] | None = None,
    ) -> None:
        self._claimed_entries = claimed_entries or []
        self._new_entries = new_entries or []
        self.acked: list[tuple[str, str, str]] = []
        self.dead_lettered: list[tuple[str, dict[str, object]]] = []

    async def xgroup_create(self, stream: str, group: str, id: str, mkstream: bool) -> bool:
        return True

    async def xautoclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        start_id: str,
        count: int | None = None,
    ) -> tuple[str, list[tuple[str, dict[str, str]]], list[str]]:
        return ("0-0", self._claimed_entries, [])

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        return [("watchlist-insight.jobs", self._new_entries)] if self._new_entries else []

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append((stream, group, message_id))
        return 1

    async def xadd(self, stream: str, payload: dict[str, object]) -> str:
        self.dead_lettered.append((stream, payload))
        return "new-id"


class CompletedWorker:
    def __init__(self, report_response: dict[str, object] | None = None) -> None:
        self.statuses: list[tuple[str, str, str | None]] = []
        self.results: list[object] = []
        self._report_response = report_response

    async def report_job(
        self, job_id: str, status: str, error: str | None = None
    ) -> dict[str, object] | None:
        self.statuses.append((job_id, status, error))
        return self._report_response

    async def analyze(self, event_id: str, analysis_version: str) -> InsightWorkerOutcome:
        return InsightWorkerOutcome(
            result={"label": "行业题材", "category": "industry_theme"}
        )

    async def write_result(self, result: object) -> None:
        self.results.append(result)


class FailingWorker(CompletedWorker):
    async def analyze(self, event_id: str, analysis_version: str) -> InsightWorkerOutcome:
        raise ValueError("LLM 不可用")


@pytest.mark.asyncio
async def test_consume_once_acknowledges_on_success() -> None:
    redis_client = FakeRedis(
        new_entries=[("1710000000000-0", {"job_id": "job-001", "event_id": "evt-001"})]
    )
    worker = CompletedWorker()
    consumer = InsightConsumer(redis_client, worker)  # type: ignore[arg-type]
    await consumer.consume_once()
    assert worker.statuses == [
        ("job-001", "processing", None),
        ("job-001", "completed", None),
    ]
    assert worker.results == [{"label": "行业题材", "category": "industry_theme"}]
    assert redis_client.acked == [(STREAM, GROUP, "1710000000000-0")]
    assert redis_client.dead_lettered == []


@pytest.mark.asyncio
async def test_consume_once_dead_letters_after_max_attempts() -> None:
    redis_client = FakeRedis(
        new_entries=[("1710000000000-0", {"job_id": "job-001", "event_id": "evt-001"})]
    )
    # Node 侧 report_job("failed") 自增 attempt 后返回 3（= insight_max_attempts）
    worker = FailingWorker({"attempt_count": 3})
    consumer = InsightConsumer(redis_client, worker)  # type: ignore[arg-type]
    await consumer.consume_once()
    assert worker.statuses == [
        ("job-001", "processing", None),
        ("job-001", "failed", "LLM 不可用"),
    ]
    assert redis_client.acked == [(STREAM, GROUP, "1710000000000-0")]
    assert redis_client.dead_lettered[0][0] == DLQ_STREAM
    payload = redis_client.dead_lettered[0][1]
    assert payload["error_code"] == "MAX_ATTEMPTS"
    assert payload["job_id"] == "job-001"


@pytest.mark.asyncio
async def test_consume_once_does_not_dead_letter_below_max_attempts() -> None:
    """失败但未达 max_attempts：不进 DLQ、不 xack，等 pending reclaim 重试。"""
    redis_client = FakeRedis(
        new_entries=[("1710000000000-0", {"job_id": "job-001", "event_id": "evt-001"})]
    )
    worker = FailingWorker({"attempt_count": 1})
    consumer = InsightConsumer(redis_client, worker)  # type: ignore[arg-type]
    await consumer.consume_once()
    assert worker.statuses == [
        ("job-001", "processing", None),
        ("job-001", "failed", "LLM 不可用"),
    ]
    assert redis_client.dead_lettered == []
    assert redis_client.acked == []


@pytest.mark.asyncio
async def test_consume_once_retries_when_snapshot_not_ready() -> None:
    redis_client = FakeRedis(
        new_entries=[("1710000000000-0", {"job_id": "job-001", "event_id": "evt-001"})]
    )

    class RetryWorker(CompletedWorker):
        async def analyze(self, event_id: str, analysis_version: str) -> InsightWorkerOutcome:
            return InsightWorkerOutcome(retryable_snapshot_not_ready=True)

    worker = RetryWorker()
    consumer = InsightConsumer(redis_client, worker)  # type: ignore[arg-type]
    await consumer.consume_once()
    assert worker.statuses == [
        ("job-001", "processing", None),
        ("job-001", "queued", None),
    ]
    # 不 ack：快照就绪后由 pending reclaim 重新执行
    assert redis_client.acked == []
    assert redis_client.dead_lettered == []


@pytest.mark.asyncio
async def test_consume_once_dead_letters_invalid_message() -> None:
    redis_client = FakeRedis(
        new_entries=[("1710000000000-0", {"event_id": "evt-001"})]
    )
    worker = CompletedWorker()
    consumer = InsightConsumer(redis_client, worker)  # type: ignore[arg-type]
    await consumer.consume_once()
    assert redis_client.dead_lettered[0][0] == DLQ_STREAM
    assert redis_client.dead_lettered[0][1]["error_code"] == "INVALID_JOB_MESSAGE"
    assert redis_client.acked == [(STREAM, GROUP, "1710000000000-0")]
    assert worker.statuses == []


@pytest.mark.asyncio
async def test_consume_once_prefers_pending_claim() -> None:
    redis_client = FakeRedis(
        claimed_entries=[
            ("1710000000000-1", {"job_id": "job-002", "event_id": "evt-002"})
        ],
        new_entries=[
            ("1710000000000-0", {"job_id": "job-001", "event_id": "evt-001"})
        ],
    )
    worker = CompletedWorker()
    consumer = InsightConsumer(redis_client, worker)  # type: ignore[arg-type]
    await consumer.consume_once()
    assert worker.statuses[0] == ("job-002", "processing", None)
    assert redis_client.acked == [(STREAM, GROUP, "1710000000000-1")]
