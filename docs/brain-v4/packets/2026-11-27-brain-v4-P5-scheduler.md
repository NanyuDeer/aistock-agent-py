# P5-scheduler 执行包

- 日期：2026-11-27
- 仓库：`d:\aistock\aistock-agent-py`（main，基线 `de88adb`）
- 开发者：NanyuDeer
- 范围：按开关注册 4 个 V4 影子定时任务（默认关闭），调用 `run_live(profile, date)` 并归档；非交易日跳过；幂等；失败重试 1 次后告警；关闭开关即回滚。

## 1. 目标

1. `brain_v4_enabled=True` 时注册 4 个影子 job（market_trace / morning / midday / evening）；
2. 每个 job 调 `run_live(profile, date)`，交易日守卫、超时、重试、幂等归档；
3. 默认全关；关闭开关即停用，不改既有 cron/job；
4. 不写生产表、不改 app-api/前端、不新增依赖、V4 import 全在函数内懒加载。

## 2. 文件

修改：

- `src/aistock_agent/config.py`：追加 8 个 `brain_v4_*` 配置项（不改既有项）
- `src/aistock_agent/services/scheduler.py`：
  - 新增 `_brain_v4_job_specs()` / `_register_brain_v4_jobs(scheduler)` / `_run_brain_v4_task(profile)`
  - `start_scheduler()` 末尾（既有 job 注册与 misfire 循环之后）按开关调用注册

新增：

- `tests/unit/brain_v4/test_scheduler_brain_v4.py`
- `docs/superpowers/packets/2026-11-27-brain-v4-P5-scheduler.md`（本文件）

修改（记录）：

- `CHANGELOG.md`

## 3. 关键契约

- 配置：`brain_v4_enabled=False`、`brain_v4_shadow_only=True`、`brain_v4_morning_cron="51 8 * * 1-5"`、`brain_v4_midday_cron="6 12 * * 1-5"`、`brain_v4_evening_cron="36 15 * * 1-5"`、`brain_v4_market_trace_cron="37 15 * * 1-5"`、`brain_v4_timeout_s=900`、`brain_v4_retry=1`。
- job id：`brain_v4_market_trace` / `brain_v4_morning` / `brain_v4_midday` / `brain_v4_evening`；每个 job `max_instances=1`、`misfire_grace_time=600`、`replace_existing=True`，cron 全部读 settings。
- `_run_brain_v4_task(profile)`：
  - 非交易日（`is_trading_day(shanghai_today())` 为 False）直接返回，不调用 runner；
  - `asyncio.wait_for(run_live(profile, date), timeout=brain_v4_timeout_s)`；
  - 超时/异常分类记日志，重试 `brain_v4_retry` 次（默认共 2 次调用），仍失败记 `brain_v4_task_gave_up` 且不抛出；
  - `status == "already_exists"` 时不再归档（同日同 profile 幂等）；
  - 归档失败只记告警，不影响主流程；
  - `run_live` / `archive_run` 均函数内 import，避免拖慢服务启动。
- 注册时机：`start_scheduler()` 中既有 job 与 `job.misfire_grace_time=3600` 全量循环之后，保证本组 600s 不被覆盖。

## 4. 不做

- 不改既有 cron 值、既有 job 函数/worker、`services/llm.py`、`features/`、app-api、前端、pyproject；
- 不新增依赖；不写生产表；不在模块顶层 import `brain_v4`。

## 5. 验证

```
uv run pytest tests/unit/brain_v4/test_scheduler_brain_v4.py -q   # 9 passed
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q    # 226 passed
uv run pytest tests/unit -q                                        # 4812 passed, 1 skipped
uv run ruff check src/aistock_agent/config.py src/aistock_agent/services/scheduler.py src/aistock_agent/brain_v4 tests  # All checks passed!
uv run mypy src/aistock_agent/brain_v4                             # 69 source files, no issues
```

## 6. 回滚

`BRAIN_V4_ENABLED=false`（默认）即不注册任何 V4 job，既有链路零影响。
