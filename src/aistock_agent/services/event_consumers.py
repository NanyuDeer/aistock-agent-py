"""Event Consumers -- evening_chain 5 个事件消费者。
事件流：
  review_quick -> ReviewQuickConsumer -> snapshot(quick)
  review_full  -> ReviewFullConsumer  -> snapshot(full) -> iterate -> broadcast
  snapshot     -> SnapshotConsumer     -> iterate（仅 full）
  iterate      -> IterateConsumer      -> broadcast
  broadcast    -> BroadcastConsumer    （终点）
"""

import asyncio
import json
from abc import ABC, abstractmethod

from structlog import get_logger

from aistock_agent.agents.workers import broadcast as broadcast_agent
from aistock_agent.agents.workers import iterate as iterate_agent
from aistock_agent.agents.workers.review import run_review
from aistock_agent.services.briefing import build_and_persist_brief
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_bus import Event, EventBus
from aistock_agent.services.snapshot_builder import build_snapshot
from aistock_agent.utils.brief_contract import (
    build_iterate_brief_summary,
    build_market_snapshot_brief_summary,
)

logger = get_logger()

# 通道名称常量
CHANNEL_REVIEW_QUICK = "review_quick"
CHANNEL_REVIEW_FULL = "review_full"
CHANNEL_SNAPSHOT = "snapshot"
CHANNEL_ITERATE = "iterate"
CHANNEL_BROADCAST = "broadcast"


class ConsumerContext:
    """消费者共享上下文。"""

    def __init__(self, event_bus: EventBus, node_api_client: object = None) -> None:
        self.event_bus = event_bus
        self.node_api = node_api_client or node_api


class BaseConsumer(ABC):
    """消费者基类。子类实现 handle。"""

    def __init__(self, ctx: ConsumerContext) -> None:
        self.ctx = ctx

    @property
    @abstractmethod
    def channel(self) -> str:
        """消费的通道名。"""

    @abstractmethod
    async def handle(self, event: Event) -> None:
        """处理事件。失败时由 run_loop 统一 retry。"""


class ReviewQuickConsumer(BaseConsumer):
    """15:30 quick review 消费者。"""

    @property
    def channel(self) -> str:
        return CHANNEL_REVIEW_QUICK

    async def handle(self, event: Event) -> None:
        report_date = event.payload["report_date"]
        trace_id = event.payload.get("trace_id", event.event_id)

        await run_review(
            report_date=report_date,
            snapshot_kind="quick",
            trace_id=trace_id,
        )

        # quick review 完成后触发 quick snapshot
        await self.ctx.event_bus.publish(
            CHANNEL_SNAPSHOT,
            payload={
                "report_date": report_date,
                "snapshot_kind": "quick",
                "trace_id": trace_id,
            },
        )
        logger.info("review_quick_done", report_date=report_date, trace_id=trace_id)


class ReviewFullConsumer(BaseConsumer):
    """20:30 full review 消费者。"""

    @property
    def channel(self) -> str:
        return CHANNEL_REVIEW_FULL

    async def handle(self, event: Event) -> None:
        report_date = event.payload["report_date"]
        trace_id = event.payload.get("trace_id", event.event_id)

        await run_review(
            report_date=report_date,
            snapshot_kind="full",
            trace_id=trace_id,
        )

        # full review 完成后触发 full snapshot -> iterate -> broadcast 完整链路
        await self.ctx.event_bus.publish(
            CHANNEL_SNAPSHOT,
            payload={
                "report_date": report_date,
                "snapshot_kind": "full",
                "trace_id": trace_id,
            },
        )
        logger.info("review_full_done", report_date=report_date, trace_id=trace_id)


class SnapshotConsumer(BaseConsumer):
    """快照消费者。quick 只存快照，full 继续触发 iterate。"""

    @property
    def channel(self) -> str:
        return CHANNEL_SNAPSHOT

    async def handle(self, event: Event) -> None:
        report_date = event.payload["report_date"]
        snapshot_kind = event.payload.get("snapshot_kind", "full")

        snapshot = await asyncio.to_thread(build_snapshot, report_date)
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            raise ValueError(f"snapshot build failed: {snapshot.get('error', 'invalid')}")

        # 持久化快照。brief_summary 由受控构造函数生成（复用 scheduler 旧链路逻辑），
        # briefing.py 对 market_snapshot 强制要求该字段，缺失则 brief_evening 降级。
        await self.ctx.node_api.save_analysis_report(
            report_type="market_snapshot",
            report_date=report_date,
            data_source="snapshot_builder",
            content={
                "brief_summary": build_market_snapshot_brief_summary(snapshot),
                "snapshot": snapshot,
                "snapshot_kind": snapshot_kind,
            },
        )

        # 仅 full snapshot 触发后续 iterate -> broadcast 链路
        if snapshot_kind == "full":
            await self.ctx.event_bus.publish(
                CHANNEL_ITERATE,
                payload={"report_date": report_date},
            )

        logger.info("snapshot_done", report_date=report_date, snapshot_kind=snapshot_kind)


class IterateConsumer(BaseConsumer):
    """迭代分析消费者。完成后触发 broadcast。"""

    @property
    def channel(self) -> str:
        return CHANNEL_ITERATE

    async def handle(self, event: Event) -> None:
        report_date = event.payload["report_date"]
        state = _make_consumer_state(report_date, intent=None)
        result = await iterate_agent.run(state)

        iterate_text = str(result.get("final_response") or "")
        iterate_payload = json.loads(iterate_text)
        if not isinstance(iterate_payload, dict):
            raise ValueError("iterate result is not valid JSON dict")

        # 原始 LLM payload 仅用于链路诊断；brief 事实由受控构造函数生成
        # （复用 scheduler 旧链路逻辑）。briefing.py 对 iterate 强制要求
        # content.brief_summary，缺失会导致 brief_evening 降级。
        await self.ctx.node_api.save_analysis_report(
            report_type="iterate",
            report_date=report_date,
            data_source="iterate_analyzer",
            content={
                "brief_summary": build_iterate_brief_summary(iterate_payload),
                "iterate_payload": iterate_payload,
            },
        )

        await self.ctx.event_bus.publish(
            CHANNEL_BROADCAST,
            payload={"report_date": report_date},
        )
        logger.info("iterate_done", report_date=report_date)


class BroadcastConsumer(BaseConsumer):
    """晚间播报消费者（链路终点）。"""

    @property
    def channel(self) -> str:
        return CHANNEL_BROADCAST

    async def handle(self, event: Event) -> None:
        report_date = event.payload["report_date"]

        brief_saved = await build_and_persist_brief("evening", report_date)
        if not brief_saved:
            raise ValueError("evening brief build/persist failed")

        state = _make_consumer_state(report_date, brief_type="evening")
        await broadcast_agent.run(state)
        logger.info("broadcast_done", report_date=report_date)


def _make_consumer_state(report_date: str, *, intent: str | None = None, brief_type: str | None = None) -> dict[str, object]:
    """构造 consumer 触发的 AgentState（trigger_source=scheduler 使报告写 DB）。"""
    state: dict[str, object] = {
        "messages": [],
        "session_id": f"event_chain_{intent or brief_type or 'report'}_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": intent,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "trigger_source": "scheduler",
        "report_date": report_date,
        "final_response": None,
    }
    if brief_type is not None:
        state["brief_type"] = brief_type
    return state


# ============================================================================
# 消费者生命周期管理
# ============================================================================

_all_tasks: list[asyncio.Task] = []


async def _consumer_loop(
    consumer: BaseConsumer,
    consumer_name: str,
    block_ms: int = 5000,
) -> None:
    """单个 consumer 的消费循环。"""
    logger.info("consumer_started", channel=consumer.channel, consumer=consumer_name)
    while True:
        try:
            events = await consumer.ctx.event_bus.consume(
                consumer.channel,
                consumer_name,
                block_ms=block_ms,
            )
            for event in events:
                try:
                    await consumer.handle(event)
                    await consumer.ctx.event_bus.ack(consumer.channel, event.event_id)
                except Exception as e:
                    logger.error(
                        "consumer_handle_failed",
                        channel=consumer.channel,
                        event_id=event.event_id,
                        error=str(e),
                        exc_info=True,
                    )
                    await consumer.ctx.event_bus.retry(event)
        except asyncio.CancelledError:
            logger.info("consumer_cancelled", channel=consumer.channel)
            raise
        except Exception as e:
            logger.error("consumer_loop_error", channel=consumer.channel, error=str(e), exc_info=True)
            await asyncio.sleep(1)


def start_all_consumers(ctx: ConsumerContext) -> list[asyncio.Task]:
    """启动全部 5 个消费者。返回 Task 列表用于管理。"""
    consumers = [
        ReviewQuickConsumer(ctx),
        ReviewFullConsumer(ctx),
        SnapshotConsumer(ctx),
        IterateConsumer(ctx),
        BroadcastConsumer(ctx),
    ]
    tasks = []
    for c in consumers:
        name = f"{c.channel}_consumer"
        task = asyncio.create_task(_consumer_loop(c, name), name=name)
        tasks.append(task)
    _all_tasks.extend(tasks)
    return tasks


async def stop_all_consumers() -> None:
    """优雅停止全部消费者。"""
    for task in _all_tasks:
        task.cancel()
    for task in _all_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _all_tasks.clear()
    logger.info("all_consumers_stopped")
