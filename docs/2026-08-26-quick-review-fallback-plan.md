# quick 链路「先重试后降级」双钩子实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 15:30 quick 链路在 review 失败时不再静默丢失晚报——瞬时故障自愈（重试）、持续故障降级（晚报照常生成并标注数据降级）。

**Architecture:** 只改两个消费者。`ReviewQuickConsumer` 对 `run_review` 做有限退避重试并把 status 透传给下游；`SnapshotConsumer` 对快照构建失败不再 raise，改构造零值降级快照继续广播，复用既有 briefing/broadcast 降级渲染。EventBus/DLQ 与 full 链路不动。

**Tech Stack:** Python 3、pytest、pytest-asyncio、pytest-mock、asyncio。

**Spec:** `docs/2026-08-26-quick-review-fallback-design.md`

## Global Constraints

- 仅改 `aistock-agent-py`（Python 侧），不改 app-api Node 侧。
- EventBus.retry / DLQ 机制不动；仅改 consumer `handle` 内部行为。
- full (20:30) 链路正常路径不动；`ReviewFullConsumer` **不**加重试（spec 1.2 只针对 quick）。
- 重试参数用**模块常量**，与既有 `PREDICTION_RETRY_BACKOFF_SEC` 惯例一致以便测试 `patch`；不新增 config.py 设置（对 spec「config.py 新增配置」的落地修正，理由：更贴合现有代码且 YAGNI）。
- 跑测试：`python -m pytest tests/unit/test_event_consumers.py -v`（项目 venv：`d:\aistock\aistock-agent-py\.venv`）。
- 禁止 `any`；degraded 仍照常进链路，不得通过抛异常让事件落入 DLQ。

---

### Task 1: ReviewQuickConsumer 退避重试 + 门控

**Files:**
- Modify: `src/aistock_agent/services/event_consumers.py:48-56`（常量区）和 `src/aistock_agent/services/event_consumers.py:99-134`（`ReviewQuickConsumer.handle`）
- Test: `tests/unit/test_event_consumers.py:338-368`（替换现有 degraded quick 测试）+ 新增两个测试

**Interfaces:**
- Consumes: `run_review(*, report_date: str, snapshot_kind: Literal["quick","full"], trace_id: str) -> ReviewRunResult`；`ReviewRunResult.status ∈ {"ok","skipped","degraded"}`（来自 `aistock_agent.agents.workers.review`）。
- Produces: 新模块常量 `REVIEW_QUICK_MAX_RETRIES: int = 3`、`REVIEW_QUICK_RETRY_BACKOFF: tuple[int, ...] = (60, 120)`；`snapshot(quick)` payload 新增 `review_degraded: bool`、`review_status: str` 字段。Task 2 消费这些常量约定（仅自己加兜底）。

- [ ] **Step 1: 改现有 degraded 测试 → 断言"重试 3 次耗尽后仍发降级 snapshot"**

先跑现有测试确认当前失败形态，然后替换 `test_review_quick_consumer_skips_review_done_on_degraded` 为以下内容（在 `tests/unit/test_event_consumers.py:338-368`）：

```python
@pytest.mark.asyncio
async def test_review_quick_consumer_degraded_exhausts_publishes_degraded_snapshot(
    mock_event_bus, mock_node_api
):
    """恒 degraded → 退避重试 3 次耗尽 → 仍发 snapshot(review_degraded=true)、不发 review_done。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-degraded",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with (
        patch(
            "aistock_agent.services.event_consumers.run_review",
            new_callable=AsyncMock,
        ) as mock_run,
        patch("aistock_agent.services.event_consumers.REVIEW_QUICK_RETRY_BACKOFF", (0, 0)),
    ):
        mock_run.return_value = ReviewRunResult(
            status="degraded",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="",
        )
        await consumer.handle(event)

    assert mock_run.await_count == 3
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["review_degraded"] is True
    assert snapshot_call.kwargs["payload"]["review_status"] == "degraded"
```

- [ ] **Step 2: 新增"重试成功后 ok"测试与"skipped 不重试"测试**

在 `tests/unit/test_event_consumers.py` 末尾（`start_all_consumers` 测试之后）追加：

```python
@pytest.mark.asyncio
async def test_review_quick_consumer_retries_then_ok(mock_event_bus, mock_node_api):
    """degraded → 按退避重试 → ok → 发 snapshot(review_degraded=false) + review_done。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-retry-ok",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with (
        patch(
            "aistock_agent.services.event_consumers.run_review",
            new_callable=AsyncMock,
        ) as mock_run,
        patch("aistock_agent.services.event_consumers.REVIEW_QUICK_RETRY_BACKOFF", (0, 0)),
    ):
        mock_run.side_effect = [
            ReviewRunResult(
                status="degraded",
                report_date="2026-07-30",
                snapshot_kind="quick",
                trace_id="t1",
                markdown="",
            ),
            ReviewRunResult(
                status="ok",
                report_date="2026-07-30",
                snapshot_kind="quick",
                trace_id="t1",
                markdown="# Quick",
            ),
        ]
        await consumer.handle(event)

    assert mock_run.await_count == 2
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["review_degraded"] is False


@pytest.mark.asyncio
async def test_review_quick_consumer_skipped_no_retry(mock_event_bus, mock_node_api):
    """skipped(已有 full) → 不重试、发 snapshot、不发 review_done。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-skipped",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="skipped",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="",
        )
        await consumer.handle(event)

    assert mock_run.await_count == 1
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert channels.count(CHANNEL_SNAPSHOT) == 1


@pytest.mark.asyncio
async def test_review_quick_consumer_ok_no_retry_publishes_review_done(
    mock_event_bus, mock_node_api
):
    """首次即 ok → 只调 1 次、发 review_done + snapshot(review_degraded=false)。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-ok",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="ok",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="# Quick",
        )
        await consumer.handle(event)

    assert mock_run.await_count == 1
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["review_degraded"] is False
```

- [ ] **Step 3: 跑新增测试，确认因常量/字段缺失而失败（RED）**

Run: `python -m pytest tests/unit/test_event_consumers.py -v`
Expected: FAIL——`REVIEW_QUICK_RETRY_BACKOFF` 属性不存在（`AttributeError`）或 `review_degraded` 键缺失（因旧实现不写这两个字段）。

- [ ] **Step 4: 加模块常量**

在 `src/aistock_agent/services/event_consumers.py` 常区块（`PREDICTION_RETRY_BACKOFF_SEC = 2` 之后，约 line 51 后）追加：

```python
# quick review 失败重试（2026-08-26 双钩子：先重试后降级）。
# 总尝试次数含首次；退避序列长度 = MAX-1，总阻塞约 3 分钟（该通道每日一次）。
REVIEW_QUICK_MAX_RETRIES = 3
REVIEW_QUICK_RETRY_BACKOFF = (60, 120)  # 秒
```

- [ ] **Step 5: 改 `ReviewQuickConsumer.handle` 加重试与门控**

替换 `src/aistock_agent/services/event_consumers.py:105-134` 的 `handle` 方法体为：

```python
    async def handle(self, event: Event) -> None:
        report_date = event.payload["report_date"]
        trace_id = event.payload.get("trace_id", event.event_id)

        # 有限退避重试：review 瞬时故障（LLM/数据源抖动）自愈，持续故障降级。
        # 为什么内联 sleep 而非 EventBus.retry：该通道一天仅一次，内联最直观、不进
        # DLQ 纠缠；后退避仅在前几次尝试间生效（2026-08-26 降级钩子设计）。
        result = None
        for attempt in range(REVIEW_QUICK_MAX_RETRIES):
            result = await run_review(
                report_date=report_date,
                snapshot_kind="quick",
                trace_id=trace_id,
            )
            if result.status in ("ok", "skipped"):
                break
            # status == "degraded"：退避后重试下一轮
            if attempt < len(REVIEW_QUICK_RETRY_BACKOFF):
                await asyncio.sleep(REVIEW_QUICK_RETRY_BACKOFF[attempt])
        review_status: str = result.status if result is not None else "degraded"
        review_degraded: bool = review_status != "ok"

        # 仅 status=ok 发布 review_done（与 ReviewFullConsumer 一致，硬约束 6）；
        # publish_review_done 内部吞掉发布异常，不阻断后续快照链路。
        if review_status == "ok" and result is not None:
            await publish_review_done(
                self.ctx.event_bus,
                report_date=result.report_date,
                trace_id=result.trace_id,
            )

        # quick review 完成后触发 quick snapshot。degraded 即使重试耗尽也照常发布，
        # 由 SnapshotConsumer 构造降级快照 → 广播，晚报不静默丢失（Task 2 兜底）。
        await self.ctx.event_bus.publish(
            CHANNEL_SNAPSHOT,
            payload={
                "report_date": report_date,
                "snapshot_kind": "quick",
                "review_degraded": review_degraded,
                "review_status": review_status,
            },
        )
        logger.info("review_quick_done", report_date=report_date, trace_id=trace_id)
```

- [ ] **Step 6: 跑全部消费者测试，确认通过（GREEN）**

Run: `python -m pytest tests/unit/test_event_consumers.py -v`
Expected: PASS（含 Task 1 新增 3 个与替换 1 个；存量 full/prediction/snapshot 测试不受影响）。

- [ ] **Step 7: Commit**

```bash
git add src/aistock_agent/services/event_consumers.py tests/unit/test_event_consumers.py
git commit -m "fix(agent): quick review 退化加退避重试并透传降级状态到快照链路"
```

---

### Task 2: SnapshotConsumer 降级快照兜底（断链源头）

**Files:**
- Modify: `src/aistock_agent/services/event_consumers.py`（`SnapshotConsumer.handle` 及模块级新增 `_degraded_snapshot`）
- Test: `tests/unit/test_event_consumers.py`（新增降级测试）

**Interfaces:**
- Consumes: `build_snapshot(date_str: str) -> dict`（可能返回 `{"error": "missing_reports", ...}` 降级结构，来自 `snapshot_builder`）；`build_market_snapshot_brief_summary(snapshot) -> dict|None`（对零值快照可产出摘要，已具备）。
- Produces: 模块级函数 `_degraded_snapshot(report_date: str, reason: str) -> dict[str, object]`（零值四维度 + `error`/`degraded`）。

- [ ] **Step 1: 新增降级快照测试（RED）**

在 `tests/unit/test_event_consumers.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_snapshot_consumer_degraded_fallback_on_missing_reports(
    mock_event_bus, mock_node_api
):
    """build_snapshot 返回 error → 不 raise、持久化降级快照、quick 仍发布 broadcast。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(
        event_id="evt-snap-degraded",
        channel="snapshot",
        payload={"report_date": "2026-07-30", "snapshot_kind": "quick"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.build_snapshot",
        return_value={"error": "missing_reports"},
    ):
        await consumer.handle(event)  # 不得抛异常

    mock_event_bus.publish.assert_called_once()
    assert mock_event_bus.publish.call_args[0][0] == CHANNEL_BROADCAST
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    # 降级快照仍能生成可播报的 brief_summary，且带降级标记
    assert kwargs["content"]["brief_summary"] is not None
    assert kwargs["content"]["snapshot"]["degraded"] is True
```

- [ ] **Step 2: 跑测试，确认因 `raise ValueError` 而失败（RED）**

Run: `python -m pytest tests/unit/test_event_consumers.py::test_snapshot_consumer_degraded_fallback_on_missing_reports -v`
Expected: FAIL——旧实现 `build_snapshot` 返回 `{"error":...}` 时 `raise ValueError`，`handle` 抛错，测试 fail。

- [ ] **Step 3: 改 `SnapshotConsumer.handle` 移除 raise 并加降级兜底**

替换 `src/aistock_agent/services/event_consumers.py:182-188` 中快照构建段为：

```python
        snapshot = await asyncio.to_thread(build_snapshot, report_date)
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            reason = (
                str(snapshot.get("error", "invalid_snapshot"))
                if isinstance(snapshot, dict)
                else "invalid_snapshot"
            )
            logger.warning(
                "snapshot_degraded_fallback",
                report_date=report_date,
                reason=reason,
            )
            # 降级快照：零值维度同样能产出 brief_summary，广播照常、不再断链。
            # 为什么不再 raise：quick 链路 review 失败时快照天然缺 review 报告，
            # 若抛错会打断广播导致晚报静默丢失（2026-08-26 双钩子设计）。
            snapshot = _degraded_snapshot(report_date, reason)
```

并在类外（`SnapshotConsumer` 定义之前，约 line 175 前）新增模块级函数：

```python
def _degraded_snapshot(report_date: str, reason: str) -> dict[str, object]:
    """快照构建失败时返回的降级快照（零值四维度 + error 标记）。

    保证 build_market_snapshot_brief_summary 能产出摘要（hit_rate=0.00），
    从而 brief_evening / 广播照常生成降级晚报。
    """
    return {
        "date": report_date,
        "error": reason,
        "degraded": True,
        "dimension_1_coverage": {
            "overlap_hits": [],
            "missing_in_morning": [],
            "over_focused": [],
            "hit_rate": 0.0,
            "new_coverage_rate": 0.0,
        },
        "dimension_2_direction": {
            "sectors": {},
            "direction_accuracy": 0.0,
            "mean_deviation": 0.0,
            "abs_mean_deviation": 0.0,
        },
        "dimension_3_attribution": {
            "sectors": {},
            "attribution_match_rate": 0.0,
        },
        "dimension_4_sentiment": {
            "morning_sentiment": 0.0,
            "review_sentiment": 0.0,
            "bias": 0.0,
        },
    }
```

- [ ] **Step 4: 跑新增测试，确认通过（GREEN）**

Run: `python -m pytest tests/unit/test_event_consumers.py::test_snapshot_consumer_degraded_fallback_on_missing_reports -v`
Expected: PASS。

- [ ] **Step 5: 跑全量消费者测试（回归保护）**

Run: `python -m pytest tests/unit/test_event_consumers.py -v`
Expected: 全部 PASS（含 Task 1 改动，无回归）。

- [ ] **Step 6: Commit**

```bash
git add src/aistock_agent/services/event_consumers.py tests/unit/test_event_consumers.py
git commit -m "fix(agent): SnapshotConsumer 快照失败降级而非断链，晚报不静默丢失"
```

---

## Execution Handoff

计划完成后交接：主 agent 调度 subagent（subagent-driven-development）或本会话内执行（executing-plans）。