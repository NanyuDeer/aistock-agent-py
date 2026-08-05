"""Redis Stream Consumer：消费 Node 发布的自选股洞察（watchlist insight）Job。"""

import asyncio
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import redis.asyncio as aioredis
import structlog
from redis.typing import EncodableT

from aistock_agent.config import settings
from aistock_agent.services.insight_client import InsightNodeClient

STREAM = "watchlist-insight.jobs"
DLQ_STREAM = "watchlist-insight.jobs.dlq"
logger = structlog.get_logger()


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _fields(value: Mapping[object, object]) -> dict[str, str]:
    return {_text(key): _text(item) for key, item in value.items()}


@dataclass
class InsightWorkerOutcome:
    """``analyze`` 返回契约：快照未就绪可重试；result 为待写入的洞察结果。"""

    retryable_snapshot_not_ready: bool = False
    result: object | None = None


class InsightWorkerProtocol(Protocol):
    """insight worker 接口（Task 11 接入真实 worker，本任务以占位可注入）。"""

    async def report_job(
        self, job_id: str, status: str, error: str | None = None
    ) -> None: ...

    async def analyze(self, event_id: str, analysis_version: str) -> InsightWorkerOutcome: ...

    async def write_result(self, result: object) -> None: ...


class InsightWorker:
    """归因 worker 占位实现 —— Task 11 替换为真实 worker。

    本任务仅打通消费骨架与 Node 状态上报（report_job 委托 InsightNodeClient）；
    analyze / write_result 尚未落地，显式抛错避免把消息误判为已完成。
    """

    def __init__(self, node_client: InsightNodeClient | None = None) -> None:
        self._node_client = node_client or InsightNodeClient()

    async def report_job(
        self, job_id: str, status: str, error: str | None = None
    ) -> None:
        await self._node_client.report_job(job_id, status, error)

    async def analyze(
        self, event_id: str, analysis_version: str
    ) -> InsightWorkerOutcome:
        raise NotImplementedError(
            f"insight worker analyze 未实现（event_id={event_id}）— Task 11 接入"
        )

    async def write_result(self, result: object) -> None:
        raise NotImplementedError("insight worker write_result 未实现 — Task 11 接入")


class InsightConsumer:
    """至少一次消费；所有结果由 Node 再校验后才会发布（同 stock_trace）。"""

    def __init__(
        self, redis_client: aioredis.Redis, worker: InsightWorkerProtocol
    ) -> None:
        self._redis = redis_client
        self._worker = worker
        self._group = settings.insight_consumer_group
        self._consumer = f"{socket.gethostname()}:{os.getpid()}"

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM, self._group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume_once(self) -> None:
        """消费最多一个新消息（pending reclaim 优先，其次新消息）。"""
        await self.ensure_group()
        claimed = await self._redis.xautoclaim(
            STREAM,
            self._group,
            self._consumer,
            settings.insight_pending_claim_idle_ms,
            "0-0",
            count=1,
        )
        claimed_entries = claimed[1] if claimed else []
        if claimed_entries:
            message_id, raw_fields = claimed_entries[0]
            await self._consume_message(_text(message_id), _fields(raw_fields))
            return
        messages = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {STREAM: ">"},
            count=1,
            block=settings.insight_consumer_block_ms,
        )
        if not messages:
            return
        _stream, entries = messages[0]
        message_id, raw_fields = entries[0]
        await self._consume_message(_text(message_id), _fields(raw_fields))

    async def _consume_message(
        self, message_id: str, fields: dict[str, str]
    ) -> None:
        job_id = fields.get("job_id", "")
        event_id = fields.get("event_id", "")
        analysis_version = fields.get("analysis_version", "watchlist-insight-v1")
        if not job_id or not event_id:
            await self._dead_letter(message_id, "INVALID_JOB_MESSAGE", fields)
            return
        await self._worker.report_job(job_id, "processing")
        try:
            outcome = await self._worker.analyze(event_id, analysis_version)
            if outcome.retryable_snapshot_not_ready:
                # 快照未就绪：不 ack，等 pending reclaim 重新执行
                await self._worker.report_job(job_id, "queued")
                return
            await self._worker.write_result(outcome.result)
            await self._worker.report_job(job_id, "completed")
            await self._redis.xack(STREAM, self._group, message_id)
            logger.info("insight_job_completed", job_id=job_id, event_id=event_id)
        except Exception as exc:
            logger.error(
                "insight_job_failed", job_id=job_id, error=str(exc), exc_info=True
            )
            await self._handle_failure(job_id, message_id, fields, str(exc))

    async def _handle_failure(
        self, job_id: str, message_id: str, fields: dict[str, str], error: str
    ) -> None:
        await self._worker.report_job(job_id, "failed", error)
        raw_attempt_count = fields.get("attempt_count", "1")
        try:
            attempt_count = int(raw_attempt_count)
        except ValueError:
            attempt_count = 1
        if attempt_count >= settings.insight_max_attempts:
            await self._dead_letter(message_id, "MAX_ATTEMPTS", fields)

    async def _dead_letter(
        self, message_id: str, error_code: str, fields: dict[str, str]
    ) -> None:
        payload: dict[EncodableT, EncodableT] = {
            "error_code": error_code,
            "failed_at": datetime.now(UTC).isoformat(),
        }
        for key, value in fields.items():
            payload[key] = value
        await self._redis.xadd(DLQ_STREAM, payload)
        await self._redis.xack(STREAM, self._group, message_id)
        logger.warning(
            "insight_job_dead_letter",
            job_id=fields.get("job_id"),
            error_code=error_code,
        )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("insight_consumer_iteration_failed")
                await asyncio.sleep(1)
