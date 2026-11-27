# Execution Packet · V4-09-P01 · 早午晚报日内链试点与接口冻结

> 本文件是已执行完毕的执行包，对应计划
> `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 9。

## 1. Packet Metadata

~~~yaml
packet_id: V4-09-P01-briefing-chain-freeze
objective: 早午晚报日内链试点（独立 run/snapshot/cutoff/ledger namespace + parent relation），renderer 只投影不新增 claim，并冻结 V4.1 公共接口基线
owner: Briefing Owner + Market Owner + Output Owner + Contract Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: a215999
feature_id: briefing
expected_commit_message: feat(brain-v4): 早午晚报日内链试点并冻结 V4.1 接口
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/features/briefing/manifest.yaml
  - src/aistock_agent/brain_v4/features/briefing/__init__.py
  - src/aistock_agent/brain_v4/features/briefing/profiles.py
  - src/aistock_agent/brain_v4/features/briefing/adapter.py
  - src/aistock_agent/brain_v4/features/briefing/projection.py
  - src/aistock_agent/brain_v4/features/briefing/evaluation.py
  - src/aistock_agent/brain_v4/renderers/__init__.py
  - src/aistock_agent/brain_v4/renderers/briefing.py
  - tests/fixtures/brain_v4/briefing/
  - tests/integration/brain_v4/test_briefing_chain.py
  - tests/unit/brain_v4/test_briefing_renderer.py
  - src/aistock_agent/brain_v4/generated/registry.lock
  - CHANGELOG.md
  - docs/superpowers/packets/2026-11-27-brain-v4-V4-09-P01-briefing-chain-freeze.md
  - docs/智慧大脑-V4-团队接入与迁移规范.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/analysts/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/testing/
  - src/aistock_agent/brain_v4/features/market_trace/
  - brain_v4_evals/
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  - pyproject.toml
~~~

实际修改文件是上述白名单的子集；`aistock-app-api`、公共 V4 契约/图/控制器/议事、
`features/market_trace`、`brain_v4_evals` 源码、pyproject、既有测试与前端均未改动。
`generated/registry.lock` 仅通过 registry CLI 重新生成。

## 3. Contracts

### Consumes

- `aistock_agent.brain_v4.contracts.briefing.BriefingEnvelope / MorningBody / MiddayBody / EveningBody / Scenario / BriefingRelation`。
- `aistock_agent.brain_v4.graph.profiles.get_profile / ledger_namespace`。
- `aistock_agent.brain_v4.features.market_trace.profile.run_market_trace`（Task 8 研判，仅按 ID 引用）。
- `brain_v4_evals.ab.run_paired.build_envelope`。
- `brain_v4_evals.reporting.generate.generate_report / ReportPaths`。

### Produces

- `features.briefing.adapter.load_briefing_chain / build_evidence_window / build_chain_windows / compute_window_sha`。
- `features.briefing.projection.project_morning / project_midday / project_evening / project_chain`。
- `features.briefing.evaluation.market_trace_reference / build_briefing_envelope / generate_briefing_reports`。
- `renderers.briefing.render`（`app/web/text/audio`）。
- `tests/fixtures/brain_v4/briefing/day_chain.json` 冻结日内链。

## 4. 关键不变量

- 早报时间泄漏隔离：`published_at` 晚于早报 cutoff 的消息、`event_time` 晚于
  `data_cutoff` 的事件均不可见；早报仍保留可证伪情景与 invalidator。
- 三个时段独立 `run_id`、`snapshot_id`、`data_cutoff` 与 ledger namespace；
  `as_of` 全部来自 fixture。
- 午报：未变化证据按同一 evidence ID 复用，只对受影响 claim 追加变化；
  每个 `ClaimChange` 携带 `parent_judgment_id` 与 `BriefingRelation`。
- 晚报：`EveningBody.market_trace_judgment_id` 真实等于 Task 8
  `run_market_trace(...).judgment.judgment_id`；晚报自身无 claim，不产生第二套主因判断。
- 四通道投影：headline、direction、relation、risk level、evidence IDs 与
  confidence 完全一致；audio 只缩短措辞，不新增 claim。
- 评测信封 parent chain：午报父=早报，晚报父=早报+午报；旧链证据 ID 与 V4 证据物理分离。

## 5. 验证结果

- 红态（实现前）：`uv run pytest tests/integration/brain_v4/test_briefing_chain.py tests/unit/brain_v4/test_briefing_renderer.py -q`
  → 收集失败 `ModuleNotFoundError: No module named 'aistock_agent.brain_v4.features.briefing.adapter'`。
- 绿态：同命令 → `13 passed in 6.03s`。
- 全量：`uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q` → `146 passed in 11.35s`。
- `uv run ruff check src/aistock_agent/brain_v4 tests` → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4` → `Success: no issues found in 53 source files`。
- `uv run python -m aistock_agent.brain_v4.registry --write-lock`：
  `registry_sha256` `433969d7a2c0ab3d17c17e479e7ff0796341f305ba67b8e3f4cd79b44e425629`
  → `5c3229e9e99a16971caf006f89a5e97714d4f765ff4dcd19f0c4d5b1b10514d9`；
  文件 SHA-256 `32E874CE5FA1B71709DFF4E210F19F2F27C53422F3ECCADE1B28AE10F0B766B0`
  → `BF876AFFCAEDE02070102649F54C818B2D476267781FE340593E7D4A1AE10F27`（已变更）。
- 报告：`D:\aistock\aistock-agent-py\brain_v4_evals\reports\briefing\index.html`
  与 `D:\aistock\aistock-agent-py\brain_v4_evals\reports\briefing\2026-11-26\briefing.html`。
  页面同时包含 `旧链 Legacy`、`<h3>V4</h3>`、`parent_judgment_ids`、
  `market_trace_judgment_id`，并出现
  `jdg_run_briefing_morning_20261126`、`jdg_run_briefing_midday_20261126`、
  `jdg_run_briefing_evening_20261126`、`jdg_run_market_trace_liquidity_01`。
  报告目录不提交。

## 6. 关键断言测试名

- 时间泄漏：`test_morning_cutoff_isolates_post_cutoff_and_future_events`。
- 三时段独立边界：`test_three_sessions_use_distinct_run_snapshot_cutoff_and_ledger_namespace`。
- 午报 parent / 增量：`test_midday_reuses_evidence_and_reopens_only_impacted_claims`。
- 晚报引用市场溯源：`test_evening_references_task8_market_trace_and_scores_every_material_claim`。
- 评测 parent chain 与报告：`test_evaluation_parent_chain_and_report_exposes_market_trace`。
- 投影一致：`test_render_preserves_shared_contract_fields_across_channels`、
  `test_audio_shortens_wording_without_adding_claims_or_confidence_drift`。

## 7. V4.1 冻结登记

`docs/智慧大脑-V4-团队接入与迁移规范.md` 新增 §11「V4.1 接口冻结基线（2026-11-27）」，
登记冻结契约清单、仅加性变更规则、红区兼容优先、registry.lock CLI 生成约束与
时段隔离不变量；顶部状态改为两条试点完成、公共接口已冻结为 V4.1 基线。

## 8. 公共接口校正记录

本包未修改任何既有 `contracts / graph / controller / deliberation / analysts / tools /
events / testing / features/market_trace` 代码，也未新增依赖或环境变量。
`manifest.yaml` 的契约/能力补充引起 lock 摘要变化，已按规范用 CLI 重新生成并提交。
