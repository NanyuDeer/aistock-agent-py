# quick 链路「先重试后降级」双钩子设计（精简版）

> 2026-08-26 因 LLM 402，15:30 `review_quick` 失败导致晚报未生成。
> 目标：让 quick 链路在 review 失败时不静默丢失晚报——瞬时故障自愈、持续故障降级。

## 1. 真正要修的两处

今天断链的唯一硬伤在消费者层。只改两个文件：

### 1.1 `SnapshotConsumer`（核心，断链源头）

现状：`build_snapshot` 返回 `{error: "missing_reports"}`（缺 review 报告）时，当前代码直接
`raise ValueError` → 广播链路在这里截断，晚报丢失。

要改成：构建出错时**不 raise**，构造零值降级快照继续链路：

```
snapshot = await asyncio.to_thread(build_snapshot, report_date)
if not isinstance(snapshot, dict) or snapshot.get("error"):
    snapshot = _degraded_snapshot(report_date, reason)   # 零值四维度 + error 标记

# 照常 persist market_snapshot → quick 仍 publish broadcast
```

依赖已验证具备：降级快照的零值维度经 `build_market_snapshot_brief_summary` 可正常产出摘要
（hit_rate=0.00），不会二次报错；`briefing`/`broadcast` 缺 review 时会输出"复盘数据暂不可用"的降级晚报。
**这些下游能力已存在，本 spec 不动。** 唯一卡点在 SnapshotConsumer 的 raise。

### 1.2 `ReviewQuickConsumer`（辅助，上游门控 + 重试）

现状：`run_review` 返回 `degraded` 也**无条件**发布 snapshot，下游空转。

要改成：加门控 + 有限退避重试：

```
for attempt in range(MAX_RETRIES):            # 3 次，退避 [60,120,240]s
    result = await run_review(quick, ...)
    if result.status in ("ok", "skipped"):    # skipped=已有 full，正常
        break
    await asyncio.sleep(BACKOFF[attempt])

publish(snapshot, {
    ..., "review_degraded": result.status != "ok", "review_status": result.status,
})
```

要点：
- `skipped`（已有 full review）视为成功，完整链路正常走，不降级。
- 瞬时故障自愈（重试成功 → 完整晚报）；持续故障（402）降级标记 → 走 1.1 的降级广播。
- 内联 sleep 阻塞该通道消费者 ≤7 分钟，一天仅触发一次，可接受。

## 2. 落地改动清单

| 文件 | 改动 |
|------|------|
| `src/aistock_agent/services/event_consumers.py` | 1. SnapshotConsumer 增加 `_degraded_snapshot` 兜底、移除 raise；2. ReviewQuickConsumer 增加门控 + 退避重试 |
| `src/aistock_agent/services/snapshot_builder.py` | 抽出可复用的降级快照构造函数（`_degraded_snapshot`） |
| `src/aistock_agent/config.py` | 新增 `review_max_retries` / `review_retry_backoff_seconds` 配置 |

## 3. 测试

- ReviewQuickConsumer：degraded→按退避重试→成功发布 `review_degraded=false`；耗尽仍发布且 `=true`；skipped 直接发布不重试。
- SnapshotConsumer：`build_snapshot` 返回 error 时不 raise、持久化降级快照、quick 仍发布 broadcast。
- brief_contract：降级快照能生成摘要（回归保护）。

## 4. 不做

- 不改 EventBus.retry / DLQ。
- 不改 full (20:30) 链路正常路径（full 分支因 1.1 受益兜底，但不加重试）。
- 不新增降级播报文案体系，复用既有 missing_sources/degraded 渲染。