"""EventBus —— 基于 Redis Stream 的事件总线。
设计要点：
- XADD/XREADGROUP/XACK 实现 at-least-once 语义
- 消费者组（consumer group）支持多消费者负载均衡
- 幂等检查（SET NX EX）防止重复处理
- 超过 max_retries 后移入死信队列（dlq:<channel>）
- XADD maxlen 限制 Stream 长度，防止内存溢出"""

import json
import logging
from dataclasses import dataclass


import redis.asyncio as aioredis
from structlog import get_logger

logger = get_logger()


@dataclass(frozen=True)
class Event:
    """事件载体。"""
    event_id: str
    channel: str
    payload: dict[str, object]
    retry_count: int = 0


class EventBus:
    """Redis Stream 事件总线。"""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        max_retries: int = 3,
        deadletter_prefix: str = "dlq:",
        consumer_group: str = "evening_chain",
        stream_max_len: int = 10000,
    ) -> None:
        self._redis = redis
        self._max_retries = max_retries
        self._dlq_prefix = deadletter_prefix
        self._group = consumer_group
        self._max_len = stream_max_len

    async def publish(
        self,
        channel: str,
        payload: dict[str, object],
        *,
        event_id: str | None = None,
    ) -> str:
        """发布事件到 Redis Stream。返回 event_id。
        如果传入 event_id，会设置幂等 key（24h TTL）。"""
        # 确保消费者组存在（首次发布时创建）
        await self._ensure_group(channel)

        # 幂等检查：如果传了 event_id 且已处理过，跳过
        if event_id is not None:
            if await self.is_processed(event_id):
                logger.info("event_bus_skip_duplicate", event_id=event_id, channel=channel)
                return event_id

        fields: dict[str, str] = {
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
        }
        if event_id is not None:
            fields["event_id"] = event_id

        redis_id = await self._redis.xadd(channel, fields, maxlen=self._max_len, approximate=True)
        event_id = event_id or str(redis_id, encoding="utf-8")

        # 标记幂等 key（24h TTL）
        await self.mark_processed(event_id, ttl_seconds=86400)

        logger.info("event_bus_published", channel=channel, event_id=event_id)
        return event_id

    async def consume(
        self,
        channel: str,
        consumer_name: str,
        *,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[Event]:
        """从消费者组读取事件。返回 Event 列表（可能为空）。"""
        await self._ensure_group(channel)

        raw = await self._redis.xreadgroup(
            self._group,
            consumer_name,
            {channel: ">"},
            count=count,
            block=block_ms,
        )

        events: list[Event] = []
        for _stream, messages in raw:
            for msg_id, fields in messages:
                payload_raw = fields.get(b"payload") or fields.get("payload")
                if payload_raw is None:
                    continue
                payload_str = str(payload_raw, encoding="utf-8") if isinstance(payload_raw, bytes) else payload_raw
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    logger.error("event_bus_invalid_payload", msg_id=str(msg_id))
                    await self.ack(channel, str(msg_id))
                    continue

                event_id = str(fields.get(b"event_id") or fields.get("event_id") or msg_id, encoding="utf-8") \
                    if isinstance(fields.get(b"event_id") or fields.get("event_id"), bytes) \
                    else str(fields.get(b"event_id") or fields.get("event_id") or msg_id)

                events.append(Event(
                    event_id=str(msg_id, encoding="utf-8") if isinstance(msg_id, bytes) else str(msg_id),
                    channel=channel,
                    payload=payload,
                    retry_count=int(payload.get("retry_count", 0)),
                ))
        return events

    async def ack(self, channel: str, event_id: str) -> None:
        """确认事件已处理。"""
        await self._redis.xack(channel, self._group, event_id)

    async def retry(self, event: Event) -> None:
        """重试事件。超过 max_retries 移入死信队列。"""
        current_retry = event.payload.get("retry_count", event.retry_count)
        new_retry_count = current_retry + 1

        if new_retry_count >= self._max_retries:
            await self.mark_deadletter(event, reason=f"max_retries_exceeded:{new_retry_count}")
            return

        payload = {**event.payload, "retry_count": new_retry_count}
        await self._redis.xadd(event.channel, payload,
                               maxlen=self._max_len, approximate=True)
        await self.ack(event.channel, event.event_id)
        logger.warning("event_bus_retry", channel=event.channel, event_id=event.event_id, retry_count=new_retry_count)

    async def mark_deadletter(self, event: Event, reason: str) -> None:
        """移入死信队列。"""
        dlq_channel = f"{self._dlq_prefix}{event.channel}"
        payload = {**event.payload, "reason": reason, "original_event_id": event.event_id}
        await self._redis.xadd(dlq_channel, {"payload": json.dumps(payload, ensure_ascii=False, default=str)},
                               maxlen=self._max_len, approximate=True)
        await self.ack(event.channel, event.event_id)
        logger.error("event_bus_deadletter", channel=event.channel, event_id=event.event_id, reason=reason)

    async def is_processed(self, event_id: str) -> bool:
        """幂等检查：event_id 是否已处理过。"""
        key = self._idempotency_key(event_id)
        result = await self._redis.get(key)
        return result is not None

    async def mark_processed(self, event_id: str, ttl_seconds: int = 86400) -> None:
        """标记 event_id 已处理（设置 TTL key）。"""
        key = self._idempotency_key(event_id)
        await self._redis.setex(key, ttl_seconds, "1")

    async def _ensure_group(self, channel: str) -> None:
        """确保消费者组存在（幂等，已存在时忽略错误）。"""
        try:
            await self._redis.xgroup_create(channel, self._group, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _idempotency_key(self, event_id: str) -> str:
        return f"event_bus:processed:{event_id}"
