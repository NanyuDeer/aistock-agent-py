"""Event Consumers -- evening_chain 事件消费者（6 个消费者，2 个消费组）。
事件流：
  review_quick -> ReviewQuickConsumer   -> snapshot(quick)
  review_full  -> ReviewFullConsumer    -> snapshot(full) -> iterate -> broadcast
                                         └-> review_done（仅 status=ok）-> PredictionConsumer
  snapshot     -> SnapshotConsumer      -> iterate（仅 full）
  iterate      -> IterateConsumer       -> broadcast
  broadcast    -> BroadcastConsumer     （终点）
  review_done  -> PredictionConsumer    （独立消费组 prediction_chain）

消费组：5 个既有消费者走默认组 evening_chain；PredictionConsumer 独立
group="prediction_chain"（大盘溯源后接预测独立拆分，PR-A/T2）。
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
from aistock_agent.services.prediction_service import (
    TraceUnavailableError,
    predict_from_trace,
    save_skipped_prediction,
)
from aistock_agent.services.snapshot_builder import build_snapshot
from aistock_agent.utils.brief_contract import (
    build_iterate_brief_summary,
    build_market_snapshot_brief_summary,
)

logger = get_logger()

# 通道名称常量
CHANNEL_REVIEW_QUICK = "review_quick"
CHANNEL_REVIEW_FULL = "review_full"
CHANNEL_REVIEW_DONE = "review_done"
CHANNEL_SNAPSHOT = "snapshot"
CHANNEL_ITERATE = "iterate"
CHANNEL_BROADCAST = "broadcast"

# PredictionConsumer 消费组（独立于 evening_chain，S1/S2）
PREDICTION_CONSUMER_GROUP = "prediction_chain"

# llm_failed/parse_failed 的 retry-once 退避秒数（S2）
PREDICTION_RETRY_BACKOFF_SEC = 2


class PredictionRetryExhaustedError(Exception):
    """预测 retry-once 后仍 llm/parse 失败：事件级失败，交由 EventBus retry → DLQ。"""


class ConsumerContext:
    """消费者共享上下文。"""

    def __init__(self, event_bus: EventBus, node_api_client: object = None) -> None:
        self.event_bus = event_bus
        self.node_api = node_api_client or node_api


class BaseConsumer(ABC):
    """消费者基类。子类实现 handle。"""

    def __init__(self, ctx: ConsumerContext) -> None:
        self.ctx = ctx

    # 消费组：None 使用 EventBus 默认组（evening_chain）；独立链路覆盖为专属组
    consumer_group: str | None = None

    @property
    @abstractmethod
    def channel(self) -> str:
        """消费的通道名。"""

    @abstractmethod
    async def handle(self, event: Event) -> None:
        """处理事件。失败时由 run_loop 统一 retry。"""


async def publish_review_done(event_bus: EventBus, *, report_date: str, trace_id: str) -> None:
    """发布 review_done（幂等 event_id=review_done_{date}_{trace_id}；失败仅告警不阻断 review）。"""
    try:
        await event_bus.publish(
            CHANNEL_REVIEW_DONE,
            payload={"report_date": report_date, "trace_id": trace_id},
            event_id=f"review_done_{report_date}_{trace_id}",
        )
        logger.info("review_done_published", report_date=report_date, trace_id=trace_id)
    except Exception as exc:
        logger.warning("review_done_publish_failed", error=str(exc), exc_info=True)


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

        result = await run_review(
            report_date=report_date,
            snapshot_kind="full",
            trace_id=trace_id,
        )

        # 仅 status=ok 发布 review_done（硬约束 6）：降级/跳过不发；
        # publish_review_done 内部吞掉发布异常，不阻断后续快照链路
        if result.status == "ok":
            await publish_review_done(
                self.ctx.event_bus,
                report_date=result.report_date,
                trace_id=result.trace_id,
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


class PredictionConsumer(BaseConsumer):
    """review_done 预测消费者（独立消费组 prediction_chain，S1/S2）。

    语义：
    - ok / gate_skipped / due_dates_failed：predict_from_trace 内已完成落库，直接收尾；
    - llm_failed / parse_failed：retry-once（退避 2s）后仍失败 → 抛异常，
      由 _consumer_loop 的 event_bus.retry(event) 接管（超过 max_retries 进 DLQ）；
    - TraceUnavailableError：不重试，落 skipped（硬约束 7）。
    """

    consumer_group = PREDICTION_CONSUMER_GROUP

    @property
    def channel(self) -> str:
        return CHANNEL_REVIEW_DONE

    async def handle(self, event: Event) -> None:
        payload = event.payload
        report_date = str(payload.get("report_date") or "")
        trace_id = str(payload.get("trace_id") or "")

        try:
            result, _ = await predict_from_trace(trace_id, report_date)
        except TraceUnavailableError as exc:
            # 溯源不可用：不重试，直接落 skipped（缓存/DB 均无法重建 trace）
            await save_skipped_prediction(f"review:{report_date}", str(exc))
            logger.warning("prediction_trace_unavailable", report_date=report_date, error=str(exc))
            return

        if result.status in {"ok", "gate_skipped", "due_dates_failed"}:
            # ok 已完整落库；gate_skipped/due_dates_failed 已落 skipped（predict_from_trace 内完成）
            logger.info("prediction_done", status=result.status, report_date=report_date)
            return

        if result.status in {"llm_failed", "parse_failed"}:
            # retry-once（指数退避），仅可重试状态（S2）
            await asyncio.sleep(PREDICTION_RETRY_BACKOFF_SEC)
            result, _ = await predict_from_trace(trace_id, report_date)
            if result.status in {"llm_failed", "parse_failed"}:
                # 事件级失败 → _consumer_loop 捕获后 event_bus.retry(event) → DLQ
                raise PredictionRetryExhaustedError(
                    f"prediction retry exhausted: status={result.status}, report_date={report_date}"
                )
            logger.info("prediction_retry_succeeded", status=result.status, report_date=report_date)
            return

        raise RuntimeError(f"unexpected prediction status: {result.status}")


def _make_consumer_state(
    report_date: str,
    *,
    intent: str | None = None,
    brief_type: str | None = None,
) -> dict[str, object]:
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
    *,
    block_ms: int = 5000,
    group: str | None = None,
) -> None:
    """单个 consumer 的消费循环。

    group：消费组名；None 使用 EventBus 默认组（evening_chain）。
    独立链路（如 prediction_chain）由 start_all_consumers 显式传入。
    """
    logger.info("consumer_started", channel=consumer.channel, consumer=consumer_name, group=group)
    while True:
        try:
            events = await consumer.ctx.event_bus.consume(
                consumer.channel,
                consumer_name,
                block_ms=block_ms,
                group=group,
            )
            for event in events:
                try:
                    await consumer.handle(event)
                    await consumer.ctx.event_bus.ack(
                        consumer.channel, event.event_id, group=event.group
                    )
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
            logger.error(
                "consumer_loop_error",
                channel=consumer.channel,
                error=str(e),
                exc_info=True,
            )
            await asyncio.sleep(1)


def start_all_consumers(ctx: ConsumerContext) -> list[asyncio.Task]:
    """启动全部 6 个消费者。返回 Task 列表用于管理。

    消费组：PredictionConsumer 走独立组 prediction_chain；
    其余 5 个不传 group（默认组 evening_chain），保持既有行为零改动。
    """
    consumers = [
        ReviewQuickConsumer(ctx),
        ReviewFullConsumer(ctx),
        SnapshotConsumer(ctx),
        IterateConsumer(ctx),
        BroadcastConsumer(ctx),
        PredictionConsumer(ctx),
    ]
    tasks = []
    for c in consumers:
        name = f"{c.channel}_consumer"
        task = asyncio.create_task(_consumer_loop(c, name, group=c.consumer_group), name=name)
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
