# 智慧大脑 V4 评测台联动实施计划（v1 草案）

> 日期：2026-11-27　状态：待评审
> 规格：`docs/brain-v4/evaluation-console-spec.md`
> 约束：不改产品前端；旧链只读且只进 `legacy_output`；V4 只写影子表；每个 Task 一个执行包。

## 执行顺序与依赖

~~~text
T1 旧链只读接口（app-api）
 -> T2 旧链适配与配对（agent-py）
 -> T3 Envelope 生成与静态站点（agent-py）
 -> T4 运行器/调度接入与验收（agent-py）
~~~

T1 完成后 T2 才能联调；T3 只依赖 `brain_v4_evals` 既有契约；T4 依赖 T2/T3。

---

## T1：app-api 旧链报告只读接口

**目标**：集中提供按日期/类型读取旧链报告的只读接口，字段裁剪，禁止写入。

**允许文件**
- 修改 `aistock-app-api/src/modules/brain-v4/BrainV4ArchiveService.ts`（新增只读查询方法，或新建 `LegacyReportService.ts`）
- 修改 `aistock-app-api/src/modules/brain-v4/internalRouter.ts`（新增 `GET /legacy-reports`）
- 修改 `aistock-app-api/src/modules/brain-v4/__tests__/internalRouter.spec.ts`
- 修改 `aistock-app-api/src/modules/brain-v4/AGENTS.md`

**接口**

~~~text
GET /internal/brain-v4/legacy-reports?date=YYYY-MM-DD[&type=morning|midday|review|broadcast|...]
->
{ code:0, data:[{ report_type, report_date, status, created_at,
                  display_report, podcast_brief, schema_version }], message:"" }
~~~

**要求**
- 只查 `agent_analysis_reports`，只读；`type` 非法返回 400；`date` 必填。
- 单条 `content` 超过 200KB 时裁剪并标 `truncated=true`。
- 不返回任何内部 prompt、token、密钥字段。

**先失败测试（红态）**
- 缺 `date` → 400；
- 正常返回至少一条 morning；
- 非法 `type` → 400；
- 返回体不含 `prompt`/`token` 字段。

**验证**
~~~bash
node --import tsx --test src/modules/brain-v4/__tests__/internalRouter.spec.ts
pnpm build
~~~

---

## T2：旧链适配与配对（agent-py）

**目标**：把旧链接口返回与 V4 `LiveRunResult` 归一化为配对单元，输出配对状态。

**允许文件**
- 新增 `brain_v4_evals/live/__init__.py`、`brain_v4_evals/live/legacy.py`、`brain_v4_evals/live/pairing.py`
- 新增 `tests/unit/brain_v4/test_live_pairing.py`
- 修改 `CHANGELOG.md`

**要点**
- `FeatureSlot` 映射集中一处：`market_trace→review`、`briefing_morning→morning`、`briefing_midday→midday`、`briefing_evening→evening|broadcast`。
- `pair(legacy_rows, v4_runs) -> list[PairedView]`，状态枚举：`PAIRED` / `DATE_ONLY` / `SNAPSHOT_MISMATCH` / `LEGACY_ONLY` / `V4_ONLY`。
- `legacy` 侧无 `snapshot_sha` 时不得伪造成一致。
- 纯函数、无网络、不读系统时间。

**先失败测试（红态）**
- 同日期同功能 → `PAIRED`；
- 旧链缺 snapshot → `DATE_ONLY`；
- 两侧 sha 不同 → `SNAPSHOT_MISMATCH` 且 `comparable is False`；
- 只有旧链 → `LEGACY_ONLY`。

**验证**
~~~bash
uv run pytest tests/unit/brain_v4/test_live_pairing.py -q
uv run pytest tests/unit/brain_v4 -q
uv run ruff check brain_v4_evals tests/unit/brain_v4
uv run mypy src/aistock_agent/brain_v4
~~~

---

## T3：Envelope 生成与静态站点

**目标**：把配对结果落成 `EvaluationEnvelope` 并复用 `generate_report` 输出双栏站点。

**允许文件**
- 新增 `brain_v4_evals/live/envelope.py`、`brain_v4_evals/live/render.py`
- 修改 `brain_v4_evals/reporting/generate.py`（仅新增可选的配对徽标与单栏分支，不改既有双栏行为）
- 新增 `tests/unit/brain_v4/test_live_envelope.py`
- 修改 `CHANGELOG.md`

**要点**
- `to_envelope(paired, v4_run, legacy_row) -> EvaluationEnvelope`：`is_shadow=True`；`legacy_output` 只放旧链；`v4_output` 放 V4；`reviewer_score` 留空。
- `SNAPSHOT_MISMATCH` 时仍生成 envelope，但 `quality_metrics` 里带 `comparable=false`，页面不显示差异结论。
- 输出目录固定 `brain_v4_evals/reports/live/`，结构与规格 §5 一致。
- 页面必须含：`LEGACY`/`V4` 栏目标签、snapshot id+sha、配对徽标、"旧链仅作对照"提示。
- 纯静态：无 fetch/XHR/CDN。

**先失败测试（红态）**
- 生成 `index.html` + `<date>/<feature>.html` + `data/<comparison_id>.json`；
- 页面文本包含 `LEGACY`、`V4`、`PAIRED`、snapshot sha；
- `SNAPSHOT_MISMATCH` 页面不含"改进/退步"字样；
- 重复生成内容一致。

**验证**
~~~bash
uv run pytest tests/unit/brain_v4/test_live_envelope.py -q
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
~~~

---

## T4：运行器/调度接入与端到端验收

**目标**：一次真实 run 后自动产出评测台页面，并可手工回放历史日期。

**允许文件**
- 修改 `src/aistock_agent/brain_v4/runtime/{runner,cli,archive}.py`（新增可选 `console_dir`，默认关闭）
- 新增 `src/aistock_agent/brain_v4/runtime/console.py`
- 修改 `src/aistock_agent/services/scheduler.py`（`_run_brain_v4_task` 归档后调用 console，失败不阻断）
- 新增 `tests/integration/brain_v4/test_console_end_to_end.py`
- 修改 `CHANGELOG.md`

**要点**
- 生成失败只记审计字段 `console_error`，不推翻 run 与归档。
- CLI 增加 `--console-dir`；`--date` 可回放历史日期。
- 调度默认开启（随 `brain_v4_enabled`），输出到固定目录。

**先失败测试（红态）**
- 假 client + 假 model 跑一次 `run_live`，目标目录出现 index 与详情页；
- 旧链接口失败时页面显示 `LEGACY_ONLY`/`—`，run 仍 `ok`；
- 生成异常时 `audit.console_error` 非空且 `status` 不变。

**验证**
~~~bash
uv run pytest tests/integration/brain_v4/test_console_end_to_end.py -q
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
uv run pytest tests/unit -q
~~~

**服务器演练**
~~~bash
# 回放一个完整交易日，确认双栏
APP_ENV=production .venv/bin/python -m aistock_agent.brain_v4.runtime.cli \
  --profile market_trace --date <trade_date> --console-dir brain_v4_evals/reports/live
~~~
验收：页面同时出现旧链 review 与 V4 market_trace，徽标为 `PAIRED`/`DATE_ONLY`。

---

## 风险与回滚

| 风险 | 处置 |
|---|---|
| 旧链 `content` 结构不稳定 | 只做字段裁剪与缺失标记，不做语义推断；解析失败显示 `—` |
| 旧链报告体积大 | 200KB 截断 + 页面折叠，不写入证据账本 |
| 评测台拖慢调度 | 生成在归档之后、异常隔离；必要时开关关闭 |
| 误把旧链当参照结论 | 页面固定提示 + `comparable` 标记 + 双栏物理隔离 |

回滚：关闭 console 开关即停止生成；已生成静态文件可整体删除，不影响影子表。

## 完成定义

1. 规格 §7 的 5 条验收全部通过；
2. `tests/unit/brain_v4` 与 `tests/integration/brain_v4` 全绿；
3. 服务器回放一个完整交易日，双栏页面可打开；
4. 红区改动（`brain_v4_evals/reporting/generate.py`）走独立执行包并附失败测试。
