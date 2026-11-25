"""事件 → 节点映射回填脚本。

为什么需要回填：
    event_node_mappings 建表后是空的。M1 用事件驱动候选时，如果每次都现算，
    既慢又会让同一个事件在不同时间算出不同映射（embedding 漂移）。
    回填一次，之后全部命中缓存。

为什么数据源是 event_entities 而不是 stock_trace_events：
    后者是股价异动事件（只有 symbol / direction / 触发价），没有文本标题，
    映射不出任何节点。语义事件在 event_entities。

可用性验证（spec 2026-11-12 §3.4）：
    第一次跑：每条事件都触发召回
    第二次跑：召回调用 **0 次**  ← 这才是「缓存优先」真正生效的证明

用法：
    python -m scripts.backfill_event_node_mappings --limit 10 --dry-run
    python -m scripts.backfill_event_node_mappings            # 全量
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_node_mapping import (
    DataClientMappingIO,
    get_event_node_mappings,
)
from aistock_agent.services.http_client import HttpClientPool

logger = structlog.get_logger(__name__)

PAGE_SIZE = 50

#: 限流间隔。1085 条并发会把 embedding 服务打挂，必须串行 + 间隔。
THROTTLE_SECONDS = 0.2


async def fetch_events(limit: int | None) -> list[dict[str, object]]:
    """分页拉取可映射事件。"""
    events: list[dict[str, object]] = []
    offset = 0
    while True:
        page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(events))
        if page_size <= 0:
            break
        payload = await node_api.get(
            f"/internal/event-entities?limit={page_size}&offset={offset}"
        )
        items = payload.get("items") if payload else None
        if not isinstance(items, list) or not items:
            break
        events.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        if limit is not None and len(events) >= limit:
            break
    return events


async def backfill(limit: int | None, dry_run: bool) -> tuple[int, int]:
    """返回 (成功数, 失败数)。单条失败不中断。"""
    events = await fetch_events(limit)
    print(f"待处理事件 {len(events)} 条（dry_run={dry_run}）")

    io = DataClientMappingIO()
    ok = 0
    failed = 0

    for index, event in enumerate(events, start=1):
        event_id = str(event.get("event_id") or "")
        title = str(event.get("title") or "").strip()
        if not event_id or not title:
            continue

        if dry_run:
            if index <= 3:
                print(f"  [dry-run] {event_id[:24]} | {title[:40]}")
            ok += 1
            continue

        try:
            mappings = await get_event_node_mappings(event_id, [title], io=io)
            confirmed = sum(1 for item in mappings if item.confirmed)
            if index % 20 == 0 or confirmed > 0:
                print(
                    f"  [{index}/{len(events)}] {event_id[:20]} "
                    f"候选 {len(mappings)} / 确认 {confirmed}"
                )
            ok += 1
        except Exception as error:  # noqa: BLE001 - 单条失败不该中断整批
            logger.warning(
                "backfill_event_failed", event_id=event_id, error=str(error)
            )
            failed += 1

        await asyncio.sleep(THROTTLE_SECONDS)

    return ok, failed


async def main() -> None:
    parser = argparse.ArgumentParser(description="回填事件→节点映射")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条")
    parser.add_argument("--dry-run", action="store_true", help="只列出不写入")
    args = parser.parse_args()

    # 脚本独立运行，没有 FastAPI 的 lifespan，必须自己初始化 HTTP 池
    await HttpClientPool.init(timeout=settings.http_timeout_seconds)
    try:
        ok, failed = await backfill(args.limit, args.dry_run)
    finally:
        await HttpClientPool.close()

    print(f"\n完成：成功 {ok} 条，失败 {failed} 条")
    if not args.dry_run:
        print("再跑一次相同命令，若召回调用为 0，说明缓存已生效（见 spec §3.4）")


if __name__ == "__main__":
    asyncio.run(main())
