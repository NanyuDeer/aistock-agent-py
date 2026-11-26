# 智慧大脑 V4 试点与交接报告

> 生成日期：2026-11-27
> 执行包：`V4-10-P01-verification-ab-gates-report`
> 前置提交：`f01aaec`
> 试行范围：`market_trace`、`briefing` 两条已冻结 V4.1 试点链
> 数据来源：`brain_v4_evals/cases/index.json` 中 20 个冻结案例，本地离线、确定性运行

## 1. 结论先行

**当前不满足灰度晋级条件，明确不推荐晋级。** 依据：

1. 时间泄漏门禁**未通过**：6 个议会案例各出现 1 条 `event_time`（15:30）
   晚于 `data_cutoff`（15:00）的定向研究证据，`time_leakage_count = 6 > 0`。
2. 最小样本量虽达 20，但 `market_trace` 的 `insufficient` 率为 **0.6**，
   议会模式在 6/6 案例以 `no_progress` 结束，质量增益缺乏正向证据。
3. 未采集人工盲评分（`reviewer_score` 全为 `None`），`quality_delta` 无法成立。
4. 回滚只在离线回放层演练，**未在真实灰度路由上演练**，`rollback_tested` 不成立。

因此按 Task 10 Step 5 要求：**门禁未真实通过前，不得推荐晋级**；本包只交付
影子评测、验证学习与门禁能力，不改变任何生产方法、不切换灰度路由。

## 2. 冻结案例集

| 类别 | 数量 | 说明 |
|---|---|---|
| briefing 完整交易日 | 10 | 每交易日含早/午/晚三时段，独立 run/snapshot/cutoff/namespace |
| market_trace 案例 | 10 | 含 4 个 `SOLO_DEEP`（局部回改）与 6 个 `ANALYST_COUNCIL` |
| 已知浅层/失败案例 | 3 | `mt_00/01/02`，`unsupported_edge_ratio = 0.333` |
| 议会 + 局部回改案例 | 1 | `mt_04`，`ANALYST_COUNCIL` + 1 次正式回改 |
| 局部回改（非议会）案例 | 1 | `mt_03`，`SOLO_DEEP` + 1 次正式回改 |

每个案例登记 `case_id`、`feature_id`、`case_sha256`、`snapshot_sha` 与
`expected_visibility`（只登记时间可见性，**不登记期望结论**）。测试
`test_each_case_file_matches_disk_and_sha_is_reproducible` 逐个复算 SHA 并断言全库不重复。

## 3. 试点样本与质量指标（真实运行结果）

样本：`market_trace` 10 个 + `briefing` 10 个交易日 = 40 个评测信封。

| 指标 | 数值 |
|---|---|
| 样本数（评测信封） | 40 |
| 盲评分 | **未采集**（`reviewer_score` 均为 `None`） |
| 研判深度 | `market_trace` 全部 D3（充分性通过且 ≥2 竞争假设） |
| 无支撑边比例 | `SOLO_DEEP` 0.333；`ANALYST_COUNCIL` 0.0 |
| 研究/议会议事轮次 | `SOLO_DEEP` 0；`ANALYST_COUNCIL` 3 |
| belief 更新数 | `SOLO_DEEP` 0；`ANALYST_COUNCIL` 每案 15 条 |
| belief 变化数（非 hold） | `SOLO_DEEP` 0；`ANALYST_COUNCIL` 每案 1 条 |
| 正式回改数 | 每个 `market_trace` 案例 1 次（共 10 次） |
| `insufficient` 率（market_trace） | 0.6（6/10） |
| 重复工具调用 | 0 |
| 时间泄漏（事件时间晚于 cutoff） | **6**（仅议会案例，每案 1 条定向研究证据） |
| 延迟 P50 / P95 / 最大 | 324 ms / 372 ms / 372 ms |
| token 总量 / 每信封 P50 | 58 150 / 1 340 |
| cost 总量 / 每信封 P95 | 0.03489 / 0.001176 |
| 午报有效变化 | 20/20（confirm 或 reverse，均属实质变化） |
| 午/晚报 parent 链正确率 | 10/10（100%） |
| 晚报评分卡覆盖 material claim | 1.0（100%） |
| 四通道投影一致性 | 通过（既有 `test_briefing_renderer` 覆盖，未回归） |

议会议案价值：6 个议会案例均产生 15 条 belief 更新与 1 次回改，但**最终仍以
`insufficient/no_progress` 收束**，说明多轮议事尚未转化为确定结论；这一点
本身构成不推荐晋级的证据。

## 4. 失败案例清单（必须明示）

| 案例 | 类别 | 问题 |
|---|---|---|
| `case_mt_00_shallow` … `case_mt_02_shallow` | 浅层 | `unsupported_edge_ratio = 0.333`，板块假设边无证据 |
| `case_mt_04_council` … `case_mt_09_standard` | 议会失败 | `decision/status = insufficient`，`stop_reason = no_progress` |
| `case_mt_04_council` … `case_mt_09_standard` | 时间泄漏 | 定向研究证据 `event_time = 15:30 > data_cutoff = 15:00` |

`case_mt_03_local_revision` 与上述议会案例结构相同，但走 `SOLO_DEEP`，
充分性通过、仅 1 条无支撑边，作为可用的“局部回改”正例保留。

## 5. 门禁评估（`brain_v4_evals.ab.gates.evaluate_promotion`）

基于本试点可确认的事实：

- 时间泄漏：6 → `time_leakage` 失败。
- snapshot SHA 一致：通过（案例 SHA 全部可复算一致）。
- 生产污染：无（全部 `is_shadow=True`，未进入正式查询）。
- 样本量：20 → 通过最小值。
- 质量增益：无盲评、无基线 → 不能证明 `quality_delta ≥ 0`。
- P95/cost：P95 372 ms、cost P95 0.001176，但无冻结基线 → 不能证明无回退。
- 回滚：仅离线确定性回放，未在灰度路由演练 → `rollback_untested`。

**结论：不推荐晋级。** 待修复时间泄漏、补齐人工盲评与灰度回滚演练后重评。

## 6. V4.1 冻结契约版本

沿用 `docs/智慧大脑-V4-团队接入与迁移规范.md §11` 的冻结基线：

- 简报：`BriefingEnvelope.v1`、`MorningBody.v1`、`MiddayBody.v1`、`EveningBody.v1`、
  `Scenario.v1`、`BriefingRelation.v1`
- 研判：`V4Judgment.v1`、`EvidenceItem.v1`、`EvidenceLedger.v1`、`Hypothesis.v1`、`CausalEdge.v1`
- 运行：`RunProfile.v1`（8 档阈值冻结）、`FeatureManifest.v1`、`generated/registry.lock`
- 评测：`EvaluationEnvelope.v1`（`is_shadow` 恒为 `True`，旧链证据不得复用为 V4 证据）

本包新增的验证/学习/门禁均为**评测侧只读能力**，不修改上述红区契约，属仅加性扩展。

## 7. 示例 Execution Packet（可直接签发给功能负责人）

```yaml
packet_id: V4-XX-P01-<feature>-shadow-pilot
objective: 将 <feature> 接入 V4 影子评测，不切换灰度路由
prerequisite_repo: aistock-agent-py
prerequisite_commit: <frozen-commit>
feature_id: <feature>
allowed_files:
  - src/aistock_agent/brain_v4/features/<feature>/**
  - brain_v4_evals/cases/<feature>/**
  - tests/integration/brain_v4/test_<feature>_*.py
  - tests/unit/brain_v4/test_<feature>_*.py
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  - pyproject.toml
verification:
  - uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
  - uv run ruff check src/aistock_agent/brain_v4 brain_v4_evals tests/unit/brain_v4 tests/integration/brain_v4
  - uv run mypy src/aistock_agent/brain_v4 brain_v4_evals
```

## 8. 回滚步骤

1. 本包**未切换灰度路由、未写生产方法**，回滚只需撤销提交：
   `git revert <v4-10-commit>`（保留历史）。
2. 若已生成离线报告：删除 `brain_v4_evals/reports/**`（报告目录本就不提交，
   不进入正式查询）。
3. 若后续引入 registry 变更：`git checkout <previous> -- src/aistock_agent/brain_v4/generated/registry.lock`，
   并重跑 `uv run python -m aistock_agent.brain_v4.registry --write-lock` 复核。
4. 确认 `select_active(...)` 仍返回空（影子隔离未被破坏）。

## 9. 首批建议迁移功能清单

| 功能 | 建议 | 理由 |
|---|---|---|
| `market_trace` | 继续影子运行，修复时间泄漏后再评估灰度 | 已有旧链/V4 双栏与回改能力 |
| `briefing`（早/午/晚） | 继续影子运行 | parent 链与四通道投影稳定，10 交易日全部复现 |
| 后续候选（trend / event / wind） | 仅签发影子执行包 | 尚未接入，无冻结案例，禁止直接灰度 |

> 任何功能在时间泄漏=0、盲评基线建立、灰度回滚演练完成后，方可重新评估晋级。

## 10. 复现方式

```bash
uv run pytest tests/unit/brain_v4/test_verification.py tests/unit/brain_v4/test_ab_gates.py -q
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
```

案例集位于 `brain_v4_evals/cases/`，SHA 与可见性由 `index.json` 冻结，
不读取系统时间、不触网。
