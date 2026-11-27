# 智慧大脑 V4 评测台联动规格（v1 草案）

> 日期：2026-11-27　状态：待评审
> 关联：`smart-brain-v4-design.md` §10 完成定义第 6 条、`team-integration-spec.md` §14
> 目标：让审计者在一处同时看到**旧链全部报告**与**V4 新链报告**，并按同一天/同一功能明确区分与配对。

## 1. 问题

当前真实 run 只写影子表并生成单栏审计页：

~~~text
run_live -> brain_v4_runs -> /internal/brain-v4/runs|briefings -> write_daily_report（单栏）
~~~

而架构承诺的是"通用静态 HTML 评测台能查看旧链/V4 双栏结果"（规范 §10 第 6 条）。缺口有三：

1. V4 run 未构造 `EvaluationEnvelope`，未调用 `brain_v4_evals.reporting.generate_report`；
2. 旧链报告（`agent_analysis_reports`）没有进入评测台，审计者看不到"同一天旧链产出了什么"；
3. 没有"同日期/同功能配对"的明确标识，无法判断是同一快照下的可比对照，还是仅同日并列。

## 2. 目标与非目标

**目标**

1. 旧链**全部**报告类型可查（morning / midday / review / broadcast / wind_leader / event_conduction / trend_score 等），按日期与类型检索。
2. V4 每次真实 run 可查，含假设、证据、缺口、回改、协作模式、token、时延、失败分类。
3. 同一 `feature_id + 日期 + 时段` 的旧链与 V4 产出一页双栏并排，**视觉与标签明确区分**。
4. 配对可信度显式化：快照一致 = 可比；快照缺失/不一致 = 仅并列，标 `snapshot_mismatch`。
5. 全部离线可打开（`file://`），无服务、无在线请求。

**非目标**

- 不改 `aistock-app-frontend` / `aistock-frontend`；
- 不把 V4 结果写进生产简报表；
- 不做实时看板、不做在线查询、不做权限系统；
- 不在本规格内做灰度切换。

## 3. 数据来源

### 3.1 旧链（只读）

| 项 | 事实 |
|---|---|
| 表 | `agent_analysis_reports`（`report_type`、`report_date`、`status`、`content` JSONB、`created_at`） |
| 现有内部读接口 | `GET /internal/analysis-reports`（`data_client.list_analysis_reports`）、`GET /internal/analysis-reports/:type/:date`（`get_analysis_report`）、复盘专用 `get_review_analysis_report` |
| 使用约束 | 只读；只作为 `legacy_output`；**不得**进入 V4 evidence、ground truth、提示词上下文 |

### 3.2 V4（只读）

| 项 | 事实 |
|---|---|
| 表 | `brain_v4_runs`（`payload` 含 judgment / evidence_ledger / hypotheses / gaps / revisions / collaboration_mode / six_factor / usage / audit） |
| 接口 | `GET /internal/brain-v4/runs`、`GET /internal/brain-v4/briefings` |
| 归档 | `POST /internal/brain-v4/archive`（按 `v4_run_id` 幂等 upsert） |

### 3.3 契约

复用现有 `brain_v4_evals/contracts.py::EvaluationEnvelope`（v1，`is_shadow` 恒 True）。不新增必填字段；如需字段一律加性、带默认值。

## 4. 配对规则

配对键：`feature_id` + `as_of` 的日期 + 时段（slot）。

| 情形 | 判定 | 展示 |
|---|---|---|
| 两侧都存在，`snapshot_sha` 相同 | 可比对照 | 双栏 + `PAIRED` 徽标 |
| 两侧都存在，旧链无 snapshot（历史报告） | 仅同日并列 | 双栏 + `DATE_ONLY` 徽标 |
| 两侧都存在，`snapshot_sha` 不同 | 不可比 | 双栏 + `SNAPSHOT_MISMATCH` 徽标，且禁止显示"改进/退步"结论 |
| 只有旧链 | 单栏 | `LEGACY_ONLY` |
| 只有 V4 | 单栏 | `V4_ONLY` |

`feature_id` 映射：`market_trace → review`；`briefing_morning → morning`；`briefing_midday → midday`；`briefing_evening → evening/broadcast`。映射表必须集中在代码一处，禁止散落。

## 5. 输出结构

~~~text
brain_v4_evals/reports/live/
  index.html                      # 按日期倒序的索引，含配对状态统计
  <date>/
    index.html                    # 当日索引：每条一行（feature / slot / 状态 / 配对徽标）
    <feature_id>.html             # 双栏或单栏详情页
    data/<comparison_id>.json     # EvaluationEnvelope 原始数据
~~~

页面必须包含：

- 顶部：日期、`as_of`、`data_cutoff`、`snapshot_id`、`snapshot_sha`、配对徽标；
- 左栏 `LEGACY`：旧链 `display_report` 摘要、report_type、生成时间；
- 右栏 `V4`：judgment decision/status/stop_reason/confidence、假设与证据计数、gaps、revisions、协作模式、六因子门禁；
- 底部：token、时延、成本、失败分类、insufficient 原因、审计者评分占位；
- 显著提示："左栏为旧链产出，仅作对照，不是 V4 证据或正确答案"。

## 6. 不变量

1. `legacy_output` 与 V4 evidence 在数据模型里分离，页面不得把旧链结论显示为"V4 依据"。
2. `is_shadow` 恒为 True；读取端只查影子表 / 只读接口。
3. 生成过程离线、幂等：同一批输入重复生成，页面与 JSON 内容一致（除生成时间）。
4. 任何字段缺失写 `null`/`—`，不得推断或补写。
5. 页面不发起任何网络请求（无 fetch/XHR/CDN）。

## 7. 验收

1. 任选一个已完成交易日，页面同时出现旧链与该日 V4 run，且徽标为 `PAIRED` 或 `DATE_ONLY`。
2. 人为构造 `snapshot_sha` 不一致案例，页面显示 `SNAPSHOT_MISMATCH` 且不出现改进结论。
3. 只有旧链的日期显示 `LEGACY_ONLY`，不出现空白的 V4 栏。
4. 断网打开页面，内容完整。
5. 旧链报告类型覆盖检查：至少 morning / midday / review / broadcast 四类可检索到。
