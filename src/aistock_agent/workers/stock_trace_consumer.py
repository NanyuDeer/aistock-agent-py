"""Redis Stream Consumer：消费 Node Outbox 发布的 Stock Trace Job。"""

import asyncio
import os
import socket
import time
from collections.abc import Mapping

import redis.asyncio as aioredis
import structlog
from redis.typing import EncodableT

from aistock_agent.agents.workers.stock_trace import StockTraceWorker
from aistock_agent.config import settings
from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.services.stock_trace_client import StockTraceNodeClient

STREAM = "stock-trace.jobs"
DLQ_STREAM = "stock-trace.jobs.dlq"
logger = structlog.get_logger()
_metrics = get_metrics_collector()


# 心跳可观测性：供 /health/ready 检查 consumer 是否卡死。
# _enabled 在 main.py lifespan 启动 consumer 时置 True；
# _last_heartbeat 在 run_forever 每次循环开头更新（含无消息的空转）。
_stock_trace_consumer_enabled = False
_stock_trace_consumer_last_heartbeat: float | None = None


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _fields(value: Mapping[object, object]) -> dict[str, str]:
    return {_text(key): _text(item) for key, item in value.items()}


# 可重投错误码白名单：结构性错误（INVALID_JOB_MESSAGE 等）不允许重投
REPLAYABLE_ERROR_CODES = {
    "SNAPSHOT_TIMEOUT",
    "NODE_WRITEBACK_FAILED",
    "WORKER_FAILED",
    "LLM_OR_DEPENDENCY_UNAVAILABLE",
}


async def replay_dlq(
    redis_client: aioredis.Redis,
    filter_criteria: dict[str, str] | None = None,
    limit: int = 50,
) -> int:
    """把 DLQ 中可重投消息 re-xadd 回主流并删除。结构性错误码直接跳过。"""
    filter_criteria = filter_criteria or {}
    messages = await redis_client.xrange(DLQ_STREAM, min="-", max="+", count=max(1, limit))
    replayed = 0
    for message_id, raw_fields in messages:
        fields = _fields(raw_fields)
        error_code = fields.get("error_code", "")
        if error_code not in REPLAYABLE_ERROR_CODES:
            continue
        if filter_criteria.get("error_code") and error_code != filter_criteria["error_code"]:
            continue
        if filter_criteria.get("job_id") and fields.get("job_id") != filter_criteria["job_id"]:
            continue
        payload: dict[EncodableT, EncodableT] = {}
        for key, value in fields.items():
            if key == "error_code":
                continue  # 重投时剔除死信元信息
            payload[key] = value
        await redis_client.xadd(STREAM, payload)
        await redis_client.xdel(DLQ_STREAM, message_id)
        replayed += 1
        if replayed >= limit:
            break
    if replayed:
        logger.info("stock_trace_dlq_replayed", count=replayed)
    return replayed


class StockTraceConsumer:
    """至少一次消费；所有结果由 Node 再校验后才会发布 Artifact。"""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        node_client: StockTraceNodeClient | None = None,
        worker: StockTraceWorker | None = None,
    ) -> None:
        self._redis = redis_client
        self._node_client = node_client or StockTraceNodeClient()
        self._worker = worker or StockTraceWorker(self._node_client)
        self._group = settings.stock_trace_consumer_group
        self._consumer = f"{socket.gethostname()}:{os.getpid()}"

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM, self._group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume_once(self) -> bool:
        """消费最多一个新消息；返回是否实际读取到消息。"""
        await self.ensure_group()
        claimed = await self._redis.xautoclaim(
            STREAM,
            self._group,
            self._consumer,
            settings.stock_trace_pending_claim_idle_ms,
            "0-0",
            count=1,
        )
        claimed_entries = claimed[1]
        if claimed_entries:
            message_id, raw_fields = claimed_entries[0]
            await self._consume_message(_text(message_id), _fields(raw_fields))
            return True
        messages = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {STREAM: ">"},
            count=1,
            block=settings.stock_trace_consumer_block_ms,
        )
        if not messages:
            return False
        _stream, entries = messages[0]
        message_id, raw_fields = entries[0]
        await self._consume_message(_text(message_id), _fields(raw_fields))
        return True

    async def _consume_message(self, message_id: str, fields: dict[str, str]) -> None:
        job_id = fields.get("job_id", "")
        event_id = fields.get("event_id", "")
        try:
            revision = int(fields["trigger_revision"])
            analysis_version = fields["analysis_version"]
        except (KeyError, ValueError):
            await self._dead_letter(message_id, fields, "INVALID_JOB_MESSAGE")
            return
        if not job_id or not event_id:
            await self._dead_letter(message_id, fields, "INVALID_JOB_MESSAGE")
            return

        # Snapshot capture is asynchronous. Entering the queue before an enriched
        # snapshot exists is expected and must not consume an LLM retry budget.
        await self._node_client.report_job(job_id, "processing")
        outcome = await self._worker.analyze(event_id, revision, analysis_version)
        if outcome.error_code != "SNAPSHOT_NOT_READY":
            await self._node_client.report_job(job_id, "processing", increment_attempt=True)
        if outcome.status == "completed" and outcome.result is not None:
            writeback = await self._node_client.write_result(outcome.result)
            if writeback is not None:
                await self._node_client.report_job(job_id, "completed")
                await self._redis.xack(STREAM, self._group, message_id)
                await self._redis.delete(f"stock_trace:snapshot_pending:{job_id}")
                logger.info("stock_trace_job_completed", job_id=job_id, event_id=event_id)
                return
            await self._handle_failure(message_id, fields, job_id, "NODE_WRITEBACK_FAILED")
            return
        if outcome.error_code == "SNAPSHOT_NOT_READY":
            await self._node_client.report_job(job_id, "queued", error_code=outcome.error_code)
            _metrics.record_stock_trace_snapshot_not_ready()
            # 超时兜底：首次命中起超过阈值仍未就绪 → SNAPSHOT_TIMEOUT 死信，杜绝无限空转
            if await self._snapshot_pending_expired(job_id):
                await self._dead_letter(message_id, fields, "SNAPSHOT_TIMEOUT")
                return
            # 不确认消息：快照就绪后由 pending reclaim 重新执行。
            return
        await self._handle_failure(
            message_id, fields, job_id, outcome.error_code or "WORKER_FAILED"
        )

    async def _snapshot_pending_expired(self, job_id: str) -> bool:
        """记录首次 SNAPSHOT_NOT_READY 时间；超过阈值返回 True（由调用方死信）。"""
        key = f"stock_trace:snapshot_pending:{job_id}"
        raw = await self._redis.get(key)
        now = int(time.time())
        if raw is None:
            await self._redis.setex(key, 3600, str(now))
            return False
        elapsed = now - int(raw)
        if elapsed > settings.stock_trace_snapshot_not_ready_timeout_seconds:
            logger.warning("stock_trace_snapshot_timeout", job_id=job_id, elapsed_seconds=elapsed)
            return True
        return False

    async def _handle_failure(
        self, message_id: str, fields: dict[str, str], job_id: str, error_code: str
    ) -> None:
        result = await self._node_client.report_job(job_id, "failed", error_code=error_code)
        raw_attempt_count = result.get("attemptCount", 0) if result else None
        attempt_count = (
            int(raw_attempt_count)
            if isinstance(raw_attempt_count, int | str)
            else settings.stock_trace_max_attempts
        )
        if attempt_count >= settings.stock_trace_max_attempts:
            await self._dead_letter(message_id, fields, error_code)

    async def _dead_letter(self, message_id: str, fields: dict[str, str], error_code: str) -> None:
        payload: dict[EncodableT, EncodableT] = {
            "error_code": error_code,
            "failed_at": "consumer",
        }
        for key, value in fields.items():
            payload[key] = value
        await self._redis.xadd(DLQ_STREAM, payload)
        job_id = fields.get("job_id")
        if job_id:
            await self._node_client.report_job(job_id, "dead_letter", error_code=error_code)
        await self._redis.xack(STREAM, self._group, message_id)
        _metrics.record_stock_trace_dlq_total(error_code)
        logger.warning("stock_trace_job_dead_letter", job_id=job_id, error_code=error_code)

    async def run_forever(self) -> None:
        global _stock_trace_consumer_last_heartbeat
        _stock_trace_consumer_last_heartbeat = time.time()
        last_inspect = 0.0
        last_alert = 0.0
        dlq_first_seen: float | None = None
        while True:
            _stock_trace_consumer_last_heartbeat = time.time()
            try:
                now = time.time()
                if now - last_inspect >= settings.stock_trace_dlq_inspect_interval_seconds:
                    last_inspect = now
                    length = await self._redis.xlen(DLQ_STREAM)
                    if length > 0:
                        if dlq_first_seen is None:
                            dlq_first_seen = now
                        if (
                            now - dlq_first_seen >= settings.stock_trace_dlq_alert_after_seconds
                            and now - last_alert >= settings.stock_trace_dlq_alert_after_seconds
                        ):
                            last_alert = now
                            logger.warning(
                                "stock_trace_dlq_alert",
                                dlq_length=length,
                                dlq_persist_seconds=int(now - dlq_first_seen),
                                hint="run POST /admin/stock-trace/dlq/replay to requeue",
                            )
                    else:
                        dlq_first_seen = None
                await self.consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stock_trace_consumer_iteration_failed")
                await asyncio.sleep(1)


async def main() -> None:
    await RedisPool.init(settings.stock_trace_redis_url, settings.redis_max_connections)
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)
    try:
        redis_client = await RedisPool.get_client()
        await StockTraceConsumer(redis_client).run_forever()
    finally:
        await HttpClientPool.close()
        await RedisPool.close()


if __name__ == "__main__":
    asyncio.run(main())
