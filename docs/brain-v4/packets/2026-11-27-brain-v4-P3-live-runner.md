# P3-live-runner 执行包

- 日期：2026-11-27
- 仓库：`d:\aistock\aistock-agent-py`（main，基线 `9377f6d`）
- 开发者：NanyuDeer
- 范围：V4 真实数据运行器 + 前端 `briefing.v1` 投影 + CLI；app-api 不动（P4）。

## 1. 目标

给定 `profile` + `date`：

1. 用 `NodeToolClient` / `NodeApiClient` 取真实只读数据；
2. 构造 V4 输入（市场溯源案例 / 早午晚链）；
3. 调用既有 V4 链（`run_market_trace`、`project_chain`）；
4. 产出 `LiveRunResult`、前端 `briefing.v1` 信封与六因子体；
5. 支持 CLI 手工运行。

## 2. 文件

新增：

- `src/aistock_agent/brain_v4/runtime/__init__.py`
- `src/aistock_agent/brain_v4/runtime/inputs.py`
- `src/aistock_agent/brain_v4/runtime/runner.py`
- `src/aistock_agent/brain_v4/runtime/briefing_projection.py`
- `src/aistock_agent/brain_v4/runtime/cli.py`
- `tests/integration/brain_v4/test_runner_real_data.py`
- `tests/unit/brain_v4/test_briefing_projection_v1.py`

修改：

- `src/aistock_agent/brain_v4/tools/tool_map.py`：`TOOL_GROUPS` + `expand_groups`
- `src/aistock_agent/brain_v4/analysts/tools.py`：按原子工具生成
- `src/aistock_agent/brain_v4/analysts/profiles.py`：网关 allowlist = 组名 + 展开原子
- `tests/unit/brain_v4/test_analyst_model_injection.py`：契约断言同步
- `CHANGELOG.md`

## 3. 关键契约

- `run_live(profile, date, *, client=None, model=None, gateway=None, archiver=None)`
- `LiveRunResult`：`run_id` / `profile` / `as_of` / `data_cutoff` / `snapshot_id` / `snapshot_sha` / `status` / `judgment` / `briefing_v1` / `six_factor` / `usage` / `audit` / `payload` / `parent_run_id` / `parent_judgment_ids`
- 时段边界：早 08:30、午 11:30/11:35、晚 15:00/15:30、market_trace 15:00/15:35（东八区，全部入参，不读系统时间）
- 幂等：进程内 `(date, profile)` 注册表；重复调用返回同一对象，不再取数
- parent：morning 无；midday→morning；evening→morning+midday，并引用 market_trace judgment id
- 降级：取数异常/超时 → `partial` 或 `insufficient`，不抛裸异常
- 证据隔离：`event_time > data_cutoff` 或 `published_at > as_of` 不进入该 run
- legacy：只放 `legacy_output`，不进入 V4 证据，也不参与完整性降级

## 4. `briefing.v1`

固定顶层字段：

```
schema_version / report_type / slot / date / as_of /
display_report{summary,highlight,sections,risks} /
podcast_brief / audio_path(None) / judgment_ids / evening_extras
```

`display_report.sections[]` 含 `id,title,conclusion,source,tags,claim_ids,judgment_id,is_headline`；投影只引用既有 claim/judgment，不新增 claim。

## 5. 验证

```
uv run pytest tests/integration/brain_v4/test_runner_real_data.py tests/unit/brain_v4/test_briefing_projection_v1.py -q
# 7 passed

uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
# 211 passed

uv run ruff check src/aistock_agent/brain_v4 tests
# All checks passed!

uv run mypy src/aistock_agent/brain_v4
# Success: no issues found in 68 source files
```

## 6. 说明

- 市场溯源复用 `run_market_trace` + `project_six_factor`；早午晚复用 `project_chain`，未改 features 既有 projection 语义。
- `fundamental` 组显式映射为空：当前 Node 内部 API 无对应端点，避免生成必失败工具。
- 为同时兼容主图按组名扇出与分析师绑定原子工具，`analyst_tool_groups` 返回“组名 + 原子工具”的并集；否则网关会把其中一边判为 `not_allowed`。
