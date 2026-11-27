# Execution Packet · V4-10-P01 · 验证学习、配对门禁与试点交接报告

> 本文件是已执行完毕的执行包，对应计划
> `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 10（最后一个实现 Task）。

## 1. Packet Metadata

~~~yaml
packet_id: V4-10-P01-verification-ab-gates-report
objective: 到期验证与错误标签（append-only）、方法候选草稿、议会质量指标、灰度晋级门禁、冻结初始案例集与试点/交接报告
owner: Learning/Eval Owner + Orchestration Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: f01aaec
feature_id: verification/learning/gates（评测侧，非产品功能）
expected_commit_message: feat(brain-v4): 验证学习、配对门禁与试点交接报告
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/verification/__init__.py
  - src/aistock_agent/brain_v4/verification/verifier.py
  - src/aistock_agent/brain_v4/verification/error_tags.py
  - src/aistock_agent/brain_v4/learning/__init__.py
  - src/aistock_agent/brain_v4/learning/candidates.py
  - src/aistock_agent/brain_v4/learning/deliberation_metrics.py
  - brain_v4_evals/ab/gates.py
  - brain_v4_evals/cases/index.json
  - brain_v4_evals/cases/briefing/
  - brain_v4_evals/cases/market_trace/
  - brain_v4_evals/reports/pilot-report.md
  - tests/unit/brain_v4/test_verification.py
  - tests/unit/brain_v4/test_ab_gates.py
  - CHANGELOG.md
  - docs/superpowers/packets/2026-11-27-brain-v4-V4-10-P01-verification-ab-gates-report.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/analysts/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/testing/
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/brain_v4/renderers/
  - tests/fixtures/brain_v4/
  - tests/integration/brain_v4/
  - brain_v4_evals/contracts.py
  - brain_v4_evals/ab/run_paired.py
  - brain_v4_evals/reporting/
  - pyproject.toml
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

实际修改文件是上述白名单的子集；既有 V4 公共代码、既有测试、既有 fixtures、
`pyproject`、`aistock-app-api` 与前端均未改动；未新增依赖或环境变量。
`brain_v4_evals/reports/**/*.html` 未提交（报告目录本就不入库）。

## 3. Contracts

### Consumes

- `aistock_agent.brain_v4.contracts`（`V4Contract`）、`contracts.judgment.V4Judgment`、
  `contracts.evidence.EvidenceItem`、`contracts.deliberation.BeliefUpdate`。
- `aistock_agent.brain_v4.features.market_trace.adapter / profile / evaluation`（只读驱动冻结案例）。
- `aistock_agent.brain_v4.features.briefing.adapter / projection / evaluation`（只读驱动完整交易日）。
- `brain_v4_evals.ab.run_paired`（配对与盲标）。

### Produces

- `verification.verifier.Verifier / verify_judgment / VerificationRecord / ObservedOutcome`。
- `verification.error_tags.classify_error`（`direction_wrong` 与 `cause_wrong` 互斥；高置信 miss 追加 `overconfidence`）。
- `learning.candidates.create_candidate / record_replay / record_ab / reject_candidate / MethodCandidate`。
- `learning.deliberation_metrics.measure_deliberation / DeliberationInputs / DeliberationMetrics`。
- `brain_v4_evals.ab.gates.evaluate_promotion / PromotionResult`。
- `brain_v4_evals.cases.index.json` 与冻结案例集、`brain_v4_evals/reports/pilot-report.md`。

## 4. 关键不变量

- 验证器 append-only：`VerificationRecord` 冻结不可变，`records` 返回只读元组，
  **绝不修改**原 judgment。
- 方向与主因分离：方向正确但主因错误只标 `cause_wrong`，不含 `direction_wrong`；
  高置信 miss 追加 `overconfidence`。
- 方法候选：`draft` 恒为 `True`、`applied_to_production` 恒为 `False`；
  `draft → replay_passed → ab_passed` 必须依次补齐回放集与配对 comparison；
  **不存在直接写生产方法的 API**。
- 灰度门禁：时间泄漏=0、snapshot SHA 一致、无生产污染、样本量 ≥20、
  质量阈值、P95/cost 回退可控、回滚已测，缺一不晋级，返回明确 `reason`。
- 冻结案例集：每个案例登记 `case_id / feature_id / case_sha256 / snapshot_sha /
  expected_visibility`（可见性，不是期望结论）；SHA 逐个可复算、全库不重复。
- 全部测试离线、确定性、不读系统时间、不触网。

## 5. 验证结果（真实运行）

- 红态（实现前）：`uv run pytest tests/unit/brain_v4/test_verification.py tests/unit/brain_v4/test_ab_gates.py -q`
  → 收集失败 `ModuleNotFoundError: No module named 'aistock_agent.brain_v4.learning'`
  与 `No module named 'brain_v4_evals.ab.gates'`。
- 绿态（focused）：同命令 → `30 passed`。
- V4 全量：`uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q` → `176 passed`。
- 全量单测：`uv run pytest tests/unit -q` → `4767 passed, 1 skipped`。
- `uv run ruff check src/aistock_agent/brain_v4 brain_v4_evals tests/unit/brain_v4 tests/integration/brain_v4`
  → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4 brain_v4_evals` → `Success: no issues found in 66 source files`。
- app-api（只读运行，未改文件）：
  `node --import tsx --test .../snapshot.spec.ts .../internalRouter.spec.ts` → `15 pass / 0 fail`；
  `pnpm build` → 成功（`tsc` 无错误）。
- 案例集：briefing 10 个完整交易日、market_trace 10 个案例（3 个已知浅层/失败、
  1 个议会+局部回改）；index 与磁盘一致、SHA 全部可复算。
- 灰度结论：**不推荐晋级**（时间泄漏 6 > 0、insufficient 率 0.6、无人工盲评、
  灰度回滚未演练）。详见 `brain_v4_evals/reports/pilot-report.md`。

## 6. 关键断言测试名

- 门禁原文断言：`test_small_sample_cannot_promote`（`sample_size=19` → `sample_too_small`）。
- 其余门禁：`test_time_leakage_blocks_promotion`、`test_snapshot_mismatch_blocks_promotion`、
  `test_production_contamination_blocks_promotion`、`test_quality_below_threshold_blocks_promotion`、
  `test_p95_regression_blocks_promotion`、`test_cost_regression_blocks_promotion`、
  `test_rollback_untested_blocks_promotion`、`test_full_pass_allows_promotion`。
- 案例集：`test_each_case_file_matches_disk_and_sha_is_reproducible`、
  `test_index_exists_and_meets_count_thresholds`、`test_expected_visibility_records_time_boundaries_not_conclusions`、
  `test_flagship_council_case_drives_real_council_and_revision`、
  `test_flagship_local_revision_case_has_revision_without_council`、
  `test_known_failure_case_is_genuinely_shallow`。
- 验证：`test_verifier_never_mutates_original_judgment`、
  `test_direction_right_cause_wrong_only_flags_cause`、`test_high_confidence_miss_adds_overconfidence`。
- 学习：`test_candidate_is_always_draft_and_never_production`、
  `test_candidate_transitions_are_gated_and_never_touch_production`、
  `test_deliberation_metrics_cover_rounds_beliefs_minority_and_leakage`。

## 7. 说明

- `aistock-app-api` 在本包开始前已存在与本包无关的未提交改动
  （`AGENTS.md`、`CHANGELOG.md`、`ThsService.ts` 等），本包只运行验证命令，
  未修改该仓库任何文件。
- 本包不修改任何生产方法、不切换灰度路由、不写入正式 ledger；学习层只产出草稿候选。
