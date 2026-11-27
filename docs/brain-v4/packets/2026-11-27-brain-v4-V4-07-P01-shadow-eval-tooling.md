# Execution Packet · V4-07-P01 · 影子归档、评测信封与执行包工具

> 本文件是已执行完毕的执行包，对应计划
> `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 7。

## 1. Packet Metadata

~~~yaml
packet_id: V4-07-P01-shadow-eval-tooling
objective: app-api 影子归档与只读对比、通用 EvaluationEnvelope 与配对运行、静态 HTML 评测台、feature 脚手架、范围校验与通用契约测试套件
owner: Output Owner + Learning/Eval Owner + Repository Maintainer
prerequisite_repo: aistock-agent-py / aistock-app-api
prerequisite_commit: f14be31 / 4e6da74
contract_baseline: v4.1-draft
feature_id: platform/eval-tooling
expected_commit_message: feat(brain-v4): 影子归档、评测信封与执行包工具 / feat(brain-v4): 影子归档与只读对比接口
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - brain_v4_evals/__init__.py
  - brain_v4_evals/contracts.py
  - brain_v4_evals/ab/__init__.py
  - brain_v4_evals/ab/run_paired.py
  - brain_v4_evals/reporting/__init__.py
  - brain_v4_evals/reporting/generate.py
  - brain_v4_evals/reporting/static/index.js
  - brain_v4_evals/reporting/static/styles.css
  - scripts/brain_v4/__init__.py
  - scripts/brain_v4/scaffold_feature.py
  - scripts/brain_v4/verify_change_scope.py
  - src/aistock_agent/brain_v4/testing/__init__.py
  - src/aistock_agent/brain_v4/testing/contract_suite.py
  - tests/unit/brain_v4/test_evaluation_envelope.py
  - tests/integration/brain_v4/test_shadow_isolation.py
  - tests/unit/brain_v4/test_execution_packet_tools.py
  - CHANGELOG.md
  - aistock-app-api/src/db/migrations/031_brain_v4_shadow.sql
  - aistock-app-api/src/modules/brain-v4/BrainV4ArchiveService.ts
  - aistock-app-api/src/modules/brain-v4/internalRouter.ts
  - aistock-app-api/src/modules/brain-v4/__tests__/internalRouter.spec.ts
  - aistock-app-api/src/modules/brain-v4/AGENTS.md
  - aistock-app-api/src/index.ts
forbidden_paths:
  - aistock-app-api/src/core/routes/internal.ts
  - aistock-app-api/src/modules/monitor/ThsService.ts
  - aistock-app-api/src/modules/quote/TencentQuoteService.ts
  - aistock-app-api/src/modules/quote/TushareKlineService.ts
  - aistock-app-api/src/shared/utils/SingleFlight.ts
  - aistock-app-api/AGENTS.md
  - aistock-app-api/CHANGELOG.md
  - aistock-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

实际修改文件是上述白名单的子集；未改 `core/routes/internal.ts`、任何既有业务模块、
根 `AGENTS.md`、app-api `CHANGELOG.md`、前端或未跟踪的 `docs/` 文件。

## 3. Contracts

### Consumes

- `aistock_agent.brain_v4.contracts.V4Contract`（`extra="forbid"` 基类）。
- `aistock-app-api` 既有 Internal API 约定：`X-Internal-Token` 与 `{code,data,message}`。
- `src/modules/brain-v4/snapshotService.ts`（Task 3）的 snapshot id / SHA 语义。

### Produces

- `brain_v4_evals.contracts.EvaluationEnvelope / select_active`。
- `brain_v4_evals.ab.run_paired.build_envelope / blind_label / SnapshotMismatchError / PairingError`。
- `brain_v4_evals.reporting.generate.generate_report / ReportPaths`。
- `scripts.brain_v4.scaffold_feature.scaffold_feature`。
- `scripts.brain_v4.verify_change_scope.verify_change_scope`。
- `aistock_agent.brain_v4.testing.contract_suite.run_contract_suite / ContractSuiteResult`。
- `aistock-app-api`：`BrainV4ArchiveService`、`brainV4InternalRouter`、`031_brain_v4_shadow.sql`。

## 4. 关键不变量

- `EvaluationEnvelope` 未知字段报错；`is_shadow` 只允许 `True`；旧链证据 ID 不得出现在 V4 evidence。
- 只有 snapshot id + SHA 完全一致才形成 comparison；盲标由 seed 确定性派生。
- 静态评测台离线可打开，无服务、无实时请求、无研究逻辑，旧链/V4 双栏。
- 脚手架只生成六个白名单条目，默认 `active_owner=legacy`、`shadow_owner=brain_v4`。
- 范围校验非空即失败；契约套件覆盖 as_of、snapshot、幂等、降级、影子、renderer claim。
- app-api 影子表独立于 `brain_run_archives`，`CHECK (is_shadow = TRUE)`；读取只查影子行。
- `POST /archive` 强制影子；comparison 快照 SHA 不一致返回 409。
- `/internal/brain-v4` 挂在 `/internal` 之后，Task 3 snapshots 仍先命中。

## 5. 验证结果

- 红态：`uv run pytest tests/unit/brain_v4/test_evaluation_envelope.py tests/integration/brain_v4/test_shadow_isolation.py tests/unit/brain_v4/test_execution_packet_tools.py -q` → 收集失败 `ModuleNotFoundError: No module named 'brain_v4_evals'` / `No module named 'aistock_agent.brain_v4.testing'`。
- 红态：`node --import tsx --test src/modules/brain-v4/__tests__/internalRouter.spec.ts` → `MODULE_NOT_FOUND`（`../BrainV4ArchiveService`）。
- 绿态：Python 三组测试 25 passed；`tests/unit/brain_v4 tests/integration/brain_v4` 119 passed。
- `uv run ruff check src/aistock_agent/brain_v4 tests brain_v4_evals scripts` → All checks passed!
- `uv run mypy src/aistock_agent/brain_v4 brain_v4_evals` → Success: no issues found in 47 source files。
- `node --import tsx --test src/modules/brain-v4/__tests__/internalRouter.spec.ts` → 9 passed。
- `pnpm build` → 通过（tsc 无错误）。
- `git status --porcelain`：两仓库仅白名单文件；app-api 用户的既有未提交项未被触碰。
