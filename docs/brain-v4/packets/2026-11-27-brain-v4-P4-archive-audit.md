# P4-archive-audit 执行包

- 日期：2026-11-27
- 仓库：
  - `d:\aistock\aistock-agent-py`（main，基线 `6384b10`）
  - `d:\aistock\aistock-app-api`（master，基线 `6e7a745`）
- 开发者：NanyuDeer
- 范围：V4 run 结果归档到 `brain_v4_runs.payload` 影子表 + 只读审计出口 + 本地每日静态审计页。

## 1. 目标

1. 把 `LiveRunResult` 收敛为固定十键 payload，POST 到 `/internal/brain-v4/archive`；
2. app-api 只读查询影子表最新当日简报，返回 `briefing_v1/usage/audit`；
3. 本地生成 `<out_dir>/<date>/index.html` 每日审计页（无 fetch/XHR）；
4. 归档失败不推翻已完成研判。

## 2. 文件

### agent-py

新增：

- `src/aistock_agent/brain_v4/runtime/archive.py`
- `tests/unit/brain_v4/test_archive_payload.py`

修改：

- `src/aistock_agent/brain_v4/runtime/cli.py`：新增 `--archive`（默认关闭）、`--report-dir`
- `CHANGELOG.md`

### app-api

修改：

- `src/modules/brain-v4/BrainV4ArchiveService.ts`：新增 `getBriefing(date, slot)`
- `src/modules/brain-v4/internalRouter.ts`：新增 `GET /briefings`
- `src/modules/brain-v4/__tests__/internalRouter.spec.ts`：新增 6 例
- `src/modules/brain-v4/AGENTS.md`

## 3. 关键契约

- `build_archive_payload(result)` 顶层为 `brain_v4_runs` 列；`payload` 固定十键：
  ```
  judgment / briefing_v1 / evidence_ledger / hypotheses / gaps / revisions /
  collaboration_mode / six_factor / usage{tokens,cost,latency_ms} /
  audit{assumptions,evidence_count,gap_count,revision_count,mode,insufficient_reasons}
  ```
- `briefing_v1` 仅 briefing profile 有值；`six_factor` 仅 market_trace 有值；缺失字段 `None`/`[]`，不编造。
- `archive_run(result, *, client=None)` 带 `X-Internal-Token`；失败分类：`timeout` / `http_error` / `request_error` / `empty_response` / `unexpected_error`，不抛裸异常。
- `write_daily_report(results, out_dir)`：`<out_dir>/<date>/index.html`，每条 run 一行（profile/status/mode/假设/证据/gap/回改/latency/tokens/cost/insufficient）。
- `GET /internal/brain-v4/briefings?date&slot`：`date` 必填，`slot ∈ pre|mid|midday|post`；只查 `brain_v4_runs` 且 `is_shadow=TRUE`、`payload ? 'briefing_v1'`，`as_of DESC` 取最新；查无 `data:null`。
- 返回结构：`{run_id, profile, date, as_of, briefing_v1, usage, audit}`。

## 4. 不做

- 不写 `agent_analysis_reports`、不改前端、不改 `core/routes/internal.ts` / `src/index.ts` / 根 AGENTS.md；
- 不新增依赖、不改表结构、不改其他模块。

## 5. 验证

```
# agent-py
uv run pytest tests/unit/brain_v4/test_archive_payload.py -q        # 6 passed
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q     # 217 passed
uv run ruff check src/aistock_agent/brain_v4 tests                  # All checks passed!
uv run mypy src/aistock_agent/brain_v4                              # 69 source files, no issues

# app-api
node --import tsx --test src/modules/brain-v4/__tests__/internalRouter.spec.ts  # 15 passed
pnpm build                                                          # tsc 通过
```
