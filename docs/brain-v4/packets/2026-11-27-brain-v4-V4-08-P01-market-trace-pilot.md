# Execution Packet · V4-08-P01 · 市场溯源试点与双栏评测报告

> 本文件是已执行完毕的执行包，对应计划
> `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 8。

## 1. Packet Metadata

~~~yaml
packet_id: V4-08-P01-market-trace-pilot
objective: 用冻结 MarketTraceSnapshot 独立重建竞争假设、因果图、补证、回改、议会与 judgment，并对同一 snapshot SHA 生成旧链/V4 双栏静态报告
owner: Market Trace Feature Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: 1f82400
feature_id: market_trace
expected_commit_message: feat(brain-v4): 市场溯源试点与双栏评测报告
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/features/market_trace/manifest.yaml
  - src/aistock_agent/brain_v4/features/__init__.py
  - src/aistock_agent/brain_v4/features/market_trace/__init__.py
  - src/aistock_agent/brain_v4/features/market_trace/profile.py
  - src/aistock_agent/brain_v4/features/market_trace/adapter.py
  - src/aistock_agent/brain_v4/features/market_trace/projection.py
  - src/aistock_agent/brain_v4/features/market_trace/evaluation.py
  - tests/fixtures/brain_v4/market_trace/
  - tests/integration/brain_v4/test_market_trace.py
  - tests/integration/brain_v4/test_market_trace_comparison.py
  - CHANGELOG.md
  - docs/superpowers/packets/2026-11-27-brain-v4-V4-08-P01-market-trace-pilot.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/analysts/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/testing/
  - brain_v4_evals/
  - src/aistock_agent/brain_v4/generated/registry.lock
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

实际修改文件是上述白名单的子集；`aistock-app-api`、公共 V4 代码、`brain_v4_evals`、
`generated/registry.lock`、pyproject、既有测试与前端均未改动。

## 3. Contracts

### Consumes

- `aistock_agent.schemas.market_trace.MarketTraceSnapshot`（冻结快照类型）。
- `aistock_agent.brain_v4.contracts.evidence.EvidenceItem / EvidenceLedger`。
- `aistock_agent.brain_v4.controller.sufficiency.evaluate_sufficiency`（门禁与结构化 Gap）。
- `aistock_agent.brain_v4.deliberation.router.route_collaboration`（协作路由）。
- `aistock_agent.brain_v4.deliberation.graph.build_council_graph`（案卷制议会）。
- `aistock_agent.brain_v4.graph.builder.commit_judgment`（唯一写者落成 V4Judgment）。
- `brain_v4_evals.ab.run_paired.build_envelope / SnapshotMismatchError`。
- `brain_v4_evals.reporting.generate.generate_report / ReportPaths`。

### Produces

- `features.market_trace.adapter.load_frozen_snapshot / snapshot_to_evidence / compute_snapshot_sha`。
- `features.market_trace.profile.run_market_trace / ResearchRun`。
- `features.market_trace.projection.project_six_factor / SixFactorChain / FactorStep`。
- `features.market_trace.evaluation.build_market_trace_envelope / build_v4_run / generate_market_trace_report`。
- `tests/fixtures/brain_v4/market_trace/liquidity_pseudo_root.json`（流动性伪根因案例）。
- `tests/fixtures/brain_v4/market_trace/close_score_council.json`（分数接近触发议会案例）。

## 4. 关键不变量

- adapter 只映射确定性事实与 provenance（三时间、来源、质量、采集错误分类），
  `supports_hypothesis_refs` 保持为空；不导入旧 review worker、旧链 state 或最终结论。
- 同一 snapshot SHA 输入产生同一 evidence 集合、同一 evidence ID 与同一摘要。
- 流动性伪根因案例：3 个竞争假设、结构化 `ResearchGap`、一次定向补证、
  一次局部回改（“流动性是放大器”），无高置信无证据因果边。
- 分数接近案例：路由 `ANALYST_COUNCIL`；R0 独立、R1 仅 role-scoped `PeerBrief`、
  `BeliefUpdate` 有证据支撑、少数意见保留、单写提交、轮次有界。
- 六因子投影只压缩已通过门禁的路径；无证据边标为假设；投影不新增 claim。
- 旧链输出只进入 `EvaluationEnvelope.legacy_output`；V4 输出与 V4 evidence 分离；
  snapshot SHA 不一致由 `build_envelope` 拒绝配对。
- 全部测试离线、确定性、`as_of` 来自 fixture、不触网、不调用真实 LLM。

## 5. 验证结果

- 红态（实现前）：`uv run pytest tests/integration/brain_v4/test_market_trace.py tests/integration/brain_v4/test_market_trace_comparison.py -q`
  → 收集失败 `ModuleNotFoundError: No module named 'aistock_agent.brain_v4.features.market_trace'`。
- 绿态：同命令 → `14 passed`。
- 全量：`uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q` → `133 passed`。
- `uv run ruff check src/aistock_agent/brain_v4 tests/integration/brain_v4 tests/fixtures/brain_v4`
  → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4` → `Success: no issues found in 46 source files`。
- 报告：`D:\aistock\aistock-agent-py\brain_v4_evals\reports\market-trace\index.html`
  （页面 `...\2026-11-27\market_trace.html`）。双栏证据：页面同时包含
  `旧链 Legacy`、`V4`、旧链结论“旧链结论：流动性收缩是今日下跌主因”、
  V4 结论“风格分化是主要驱动”、`ANALYST_COUNCIL` 与 snapshot SHA。
  报告目录不提交。

## 6. 公共接口校正记录

本包不需要修改任何公共契约、graph、controller、deliberation、analysts、tools、
events、`brain_v4_evals` 或 `generated/registry.lock`。失败案例：无。

执行中识别到两处非公共契约偏差，均以“不改公共代码 / 不放宽测试”方式处理：

1. **未保留 `features/__init__.py`**
   - 现象：一旦创建 `features/__init__.py`，Python 会生成 `features/__pycache__/`；
     既有 `tests/unit/brain_v4/test_registry.py::test_repo_registry_is_valid_and_lock_is_fresh`
     扫描 `brain_v4/features` 的立即子目录时报告
     `RegistryError(code='missing_manifest', message='features/__pycache__ has no manifest.yaml')`。
   - 处理：`features/` 作为 PEP 420 命名空间包使用，保留
     `features/market_trace/__init__.py`；`import aistock_agent.brain_v4.features.market_trace.*`
     正常工作，既有 registry 测试保持绿色。
   - 最小变更建议：若后续要求 `features/` 成为常规包，应在拥有 `registry.py` 修改权限的
     Task 中让 registry 扫描显式跳过 `__pycache__`，并同步更新 lock，而不是在本包放宽测试。

2. **`manifest.yaml` 未改动**
   - 现象：Task 2 已完整声明 `profiles / contracts / capabilities / degradation`；
     额外登记 `EvaluationEnvelope.v1` 会改变 manifest 摘要，使
     `generated/registry.lock` 过期，而本包禁止手改 lock 与公共代码。
   - 处理：保留 Task 2 的 manifest 原样；`EvaluationEnvelope` 由评测层产生，
     不属于 feature 图输出契约。
   - 最小变更建议：若确需登记 `EvaluationEnvelope.v1`，在拥有 lock 更新权限的
     Task 中修改 manifest 并运行 registry lock 生成命令。
