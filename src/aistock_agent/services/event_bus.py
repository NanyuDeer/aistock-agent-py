"""EventBus —— 基于 Redis Stream 的事件总线。
设计要点：
- XADD/XREADGROUP/XACK 实现 at-least-once 语义
- 消费者组（consumer group）支持多消费者负载均衡
- 幂等检查（SET NX EX）防止重复处理
- 超过 max_retries 后移入死信队列（dlq:<channel>）
- XADD maxlen 限制 Stream 长度，防止内存溢出
- PEL 恢复：XAUTOCLAIM（不可用时降级 XPENDING + XCLAIM）认领超时空闲消息，
  避免「读到消息到 XACK 之间进程崩溃」导致的静默丢事件（spec §13.6）"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import redis.asyncio as aioredis
from structlog import get_logger

from aistock_agent.config import settings

logger = get_logger()


@dataclass(frozen=True)
class Event:
    """事件载体。"""
    event_id: str
    channel: str
    payload: dict[str, object]
    group: str  # 事件所属消费组（每个事件都来自某个消费组，必填）
    retry_count: int = 0


# Redis Stream 原始条目：(msg_id, fields)，xreadgroup / xautoclaim / xclaim 同形
StreamEntry = tuple[object, Mapping[object, object]]


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
        group: str | None = None,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[Event]:
        """从消费者组读取事件。返回 Event 列表（可能为空）。
        未传 group 时使用总线默认消费者组（self._group）。"""
        if group is None:
            group = self._group
        await self._ensure_group(channel, group=group)

        raw = await self._redis.xreadgroup(
            group,
            consumer_name,
            {channel: ">"},
            count=count,
            block=block_ms,
        )

        entries: list[StreamEntry] = []
        for _stream, messages in raw:
            entries.extend(messages)
        return await self._parse_entries(channel, group, entries)

    async def reclaim_pending(
        self,
        channel: str,
        consumer_name: str,
        *,
        group: str | None = None,
        min_idle_ms: int | None = None,
        count: int | None = None,
    ) -> list[Event]:
        """认领本消费者组 PEL 中空闲超过阈值的消息，供调用方重新处理。

        消费进程在「读到消息」与 XACK 之间崩溃/重启时，该消息会滞留在组 PEL 中：
        XREADGROUP(">") 只投递新消息，滞留消息不会被任何消费者拿到 → 静默丢事件。
        此处用 XAUTOCLAIM 把空闲超过 min_idle_ms 的消息改派给本消费者；XAUTOCLAIM
        不可用（Redis < 6.2 / 老客户端）时降级 XPENDING + XCLAIM。

        返回的 Event 与 consume 同形，由调用方复用既有处理分支（处理成功才 XACK；
        失败则不 XACK，留在 PEL 等下次认领）。任何失败只告警并返回空列表：总线
        不可用不得影响主链路（消费循环）。
        """
        if group is None:
            group = self._group
        if min_idle_ms is None:
            min_idle_ms = settings.event_bus_pel_min_idle_ms
        if count is None:
            count = settings.event_bus_pel_reclaim_batch
        try:
            await self._ensure_group(channel, group=group)
            entries = await self._claim_stale_entries(
                channel, group, consumer_name, min_idle_ms, count
            )
            events = await self._parse_entries(channel, group, entries)
        except Exception as exc:  # noqa: BLE001 — 总线不可用不得影响主链路
            logger.warning(
                "event_bus_pel_reclaim_failed",
                channel=channel,
                group=group,
                min_idle_ms=min_idle_ms,
                error=str(exc),
                exc_info=True,
            )
            return []
        if events:
            logger.info(
                "event_bus_pel_reclaimed",
                channel=channel,
                group=group,
                count=len(events),
                min_idle_ms=min_idle_ms,
            )
        return events

    async def _claim_stale_entries(
        self,
        channel: str,
        group: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamEntry]:
        """认领超时空闲条目：XAUTOCLAIM 优先，不可用时降级 XPENDING + XCLAIM。"""
        try:
            claimed = await self._redis.xautoclaim(
                channel, group, consumer_name, min_idle_ms, "0-0", count=count
            )
        except Exception as exc:  # noqa: BLE001 — 老版本 Redis/客户端无 XAUTOCLAIM
            logger.warning(
                "event_bus_pel_xautoclaim_unavailable",
                channel=channel,
                group=group,
                error=str(exc),
            )
            return await self._claim_stale_entries_via_xpending(
                channel, group, consumer_name, min_idle_ms, count
            )
        # redis-py 5.x 归一后为 [next_cursor, [(msg_id, fields)], deleted_ids]；
        # Redis 6.2 无第三项，故按索引取消息列表而不解构
        if not claimed or len(claimed) < 2:
            return []
        return list(claimed[1])

    async def _claim_stale_entries_via_xpending(
        self,
        channel: str,
        group: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamEntry]:
        """降级路径：XPENDING 按 idle 过滤取待认领 id → XCLAIM 改派给本消费者。"""
        pending = await self._redis.xpending_range(
            channel, group, "-", "+", count, idle=min_idle_ms
        )
        # XPENDING 明细每项为 [msg_id, consumer, idle_ms, delivery_count]
        message_ids = [entry[0] for entry in pending or []]
        if not message_ids:
            return []
        claimed = await self._redis.xclaim(
            channel, group, consumer_name, min_idle_ms, message_ids
        )
        return list(claimed or [])

    async def _parse_entries(
        self,
        channel: str,
        group: str,
        entries: Iterable[StreamEntry],
    ) -> list[Event]:
        """把 Redis Stream 原始条目归一为 Event（新消息与 PEL 认领共用）。

        不可解析的 payload 在此统一丢弃（error 日志 + XACK，与既有语义一致）：
        否则毒消息会一直留在 PEL 里被反复认领。
        """
        events: list[Event] = []
        for msg_id, fields in entries:
            # 归一消息 id：redis-py 默认 decode_responses=False，msg_id 为 bytes，
            # str(bytes) 会得到 "b'1234-0'" 而非真实 id（XACK 将匹配不到消息）
            message_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
            payload_raw = fields.get(b"payload") or fields.get("payload")
            if payload_raw is None:
                continue
            payload_str = (
                payload_raw.decode("utf-8")
                if isinstance(payload_raw, bytes)
                else str(payload_raw)
            )
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                logger.error("event_bus_invalid_payload", msg_id=message_id)
                await self.ack(channel, message_id, group=group)
                continue

            events.append(
                Event(
                    event_id=message_id,
                    channel=channel,
                    payload=payload,
                    retry_count=int(payload.get("retry_count", 0)),
                    group=group,
                )
            )
        return events

    async def ack(self, channel: str, event_id: str, *, group: str | None = None) -> None:
        """确认事件已处理。未传 group 时使用总线默认消费者组（self._group）。"""
        if group is None:
            group = self._group
        await self._redis.xack(channel, group, event_id)

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
        await self.ack(event.channel, event.event_id, group=event.group)
        logger.warning(
            "event_bus_retry",
            channel=event.channel,
            event_id=event.event_id,
            retry_count=new_retry_count,
        )

    async def mark_deadletter(self, event: Event, reason: str) -> None:
        """移入死信队列。"""
        dlq_channel = f"{self._dlq_prefix}{event.channel}"
        payload = {**event.payload, "reason": reason, "original_event_id": event.event_id}
        await self._redis.xadd(
            dlq_channel,
            {"payload": json.dumps(payload, ensure_ascii=False, default=str)},
            maxlen=self._max_len,
            approximate=True,
        )
        # 用事件所属消费组 ack，否则默认组 xack 使该组消息永久 pending
        await self.ack(event.channel, event.event_id, group=event.group)
        logger.error(
            "event_bus_deadletter",
            channel=event.channel,
            event_id=event.event_id,
            reason=reason,
        )

    async def is_processed(self, event_id: str) -> bool:
        """幂等检查：event_id 是否已处理过。"""
        key = self._idempotency_key(event_id)
        result = await self._redis.get(key)
        return result is not None

    async def mark_processed(self, event_id: str, ttl_seconds: int = 86400) -> None:
        """标记 event_id 已处理（设置 TTL key）。"""
        key = self._idempotency_key(event_id)
        await self._redis.setex(key, ttl_seconds, "1")

    async def _ensure_group(self, channel: str, *, group: str | None = None) -> None:
        """确保消费者组存在（幂等，已存在时忽略错误）。
        未传 group 时使用总线默认消费者组（self._group）。"""
        if group is None:
            group = self._group
        try:
            await self._redis.xgroup_create(channel, group, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _idempotency_key(self, event_id: str) -> str:
        return f"event_bus:processed:{event_id}"


# ============================================================================
# 默认总线访问器（供 review.run() 双保险补发 review_done 使用，后续任务消费）
# ============================================================================

_default_bus: EventBus | None = None


def set_default_bus(bus: EventBus | None) -> None:
    """设置全局默认 EventBus 实例（None 表示清除）。"""
    global _default_bus
    _default_bus = bus


def get_default_bus() -> EventBus | None:
    """获取全局默认 EventBus 实例（未设置时返回 None）。"""
    return _default_bus
