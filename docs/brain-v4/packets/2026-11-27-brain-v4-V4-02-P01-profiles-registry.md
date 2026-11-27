# Execution Packet · V4-02-P01 · Profiles、运行隔离与特性注册表

> 本文件是**已填写并执行完毕**的执行包（由 `brain-v4-task-packet-template.md` 派生），
> 对应计划 `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 2。

## 1. Packet Metadata

~~~yaml
packet_id: V4-02-P01-profiles-registry
objective: 建立不可变 RunProfile 注册表、确定性停止策略、FeatureManifest 与分布式 registry 及可复现 registry.lock
owner: Orchestration Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: 308806e
contract_baseline: v4.1-draft
feature_id: platform/orchestration
expected_commit_message: feat(brain-v4): profiles、停止策略与特性注册表
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/controller/__init__.py
  - src/aistock_agent/brain_v4/controller/stop_policy.py
  - src/aistock_agent/brain_v4/contracts/manifest.py
  - src/aistock_agent/brain_v4/contracts/research.py   # 仅追加 RoundLedger 两可选字段
  - src/aistock_agent/brain_v4/registry.py
  - src/aistock_agent/brain_v4/features/briefing/manifest.yaml
  - src/aistock_agent/brain_v4/features/market_trace/manifest.yaml
  - src/aistock_agent/brain_v4/generated/registry.lock
  - tests/unit/brain_v4/test_profiles.py
  - tests/unit/brain_v4/test_stop_policy.py
  - tests/unit/brain_v4/test_registry.py
  - CHANGELOG.md
forbidden_paths:
  - src/aistock_agent/brain_v4/graph/state.py
  - src/aistock_agent/brain_v4/contracts/（research.py 的两个追加字段除外）
  - pyproject.toml
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  - tests/unit/（brain_v4 子目录除外）
~~~

规则：实际修改文件必须是 `allowed_files` 的子集；`registry.lock` 只能由 `--write-lock` 生成。

## 3. Contracts

### Consumes

- `contracts.research.RoundLedger`：append-only 研究轮账本；本 Packet 追加
  `sufficiency_passed: bool = False`、`oscillation_detected: bool = False`，不改既有字段与校验。
- `contracts.V4Contract`：`extra="forbid"` 基类。

### Produces

- `graph.profiles.RunProfile`（frozen）：8 个档位，显式声明
  `max_research_rounds / max_deliberation_rounds / analyst_max_steps / max_revisions /
  timeout_s / token_budget / tool_groups / data_cutoff_policy / requires_parent_judgment /
  required_subprofiles / output_contract / allow_case_memory`。
- `graph.profiles.get_profile(name)`、`ledger_namespace(profile, run_id)`。
- `controller.stop_policy.BudgetState`、`StopDecision`、`evaluate_stop(ledger, budget)`。
- `contracts.manifest.FeatureManifest` 及 `ManifestBudget / ManifestDegradation / LegacyDeletion`。
- `registry.Registry`、`RegistryEntry`、`RegistryError`、`load_registry`、
  `validate_registry`、`generate_registry_lock`、CLI `--write-lock / --check`。

### Invariants

- Profile 冻结，节点不得读取 profile 外的默认值。
- 停止判定顺序固定：sufficient > stall(>=2) > unstable > budget > continue。
- 同一输出在 brain_v4 下最多一个 active owner；目录与 manifest 双向一致；依赖无环。
- `registry.lock` 对同一组 manifest 逐字节可复现；禁止手改。

## 4. Required Context

1. `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` 第 226-308 行（Task 2）。
2. `docs/superpowers/plans/brain-v4-task-packet-template.md`。
3. `src/aistock_agent/brain_v4/contracts/__init__.py`、`contracts/research.py`、
   `contracts/deliberation.py`、`graph/state.py`。
4. `docs/智慧大脑-V4-团队接入与迁移规范.md` §3.1、§4、§5。
5. `docs/superpowers/specs/2026-11-26-smart-brain-v4-design.md` §9（时间档位）。

## 5. Failing Test First

测试文件：`tests/unit/brain_v4/test_profiles.py`（计划 Step 1 三个用例）、
`tests/unit/brain_v4/test_stop_policy.py`（五分支）、
`tests/unit/brain_v4/test_registry.py`（重复 id、依赖环、双 active owner、缺 manifest、
缺目录、确定性 lock、仓库 lock 新鲜度）。

运行与预期红：

~~~text
# profiles
ModuleNotFoundError: No module named 'aistock_agent.brain_v4.graph.profiles'

# stop policy
ModuleNotFoundError: No module named 'aistock_agent.brain_v4.controller'

# registry
ImportError: cannot import name 'registry' from 'aistock_agent.brain_v4'
~~~

以上三者均已真实复现。

## 6. Implementation Steps

1. 写 `test_profiles.py`，确认红；实现 `graph/profiles.py`，3 passed。
2. 写 `test_stop_policy.py`，确认红；为 `RoundLedger` 追加两可选字段，
   新建 `controller/__init__.py` 与 `controller/stop_policy.py`，8 passed（含 profile）。
3. 写 `test_registry.py`，确认红；实现 `contracts/manifest.py` 与 `registry.py`，
   新增两个真实 manifest，`--write-lock` 生成 lock，7 passed。

## 7. Verification

~~~text
uv run pytest tests/unit/brain_v4/test_profiles.py tests/unit/brain_v4/test_stop_policy.py tests/unit/brain_v4/test_registry.py -q
uv run pytest tests/unit/brain_v4 -q
uv run python -m aistock_agent.brain_v4.registry --write-lock
git diff --stat
uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
uv run mypy src/aistock_agent/brain_v4
~~~

真实结果：

- 三文件 pytest：`15 passed`（3 + 5 + 7）。
- 全量 brain_v4：`26 passed`。
- `--write-lock` 连续两次：`registry_sha256: 433969d7a2c0ab3d17c17e479e7ff0796341f305ba67b8e3f4cd79b44e425629`。
- `ruff`：`All checks passed!`。
- `mypy`：`Success: no issues found in 15 source files`。
- `git status --porcelain`：仅 1 个修改文件 + 11 个白名单内新增文件。

## 8. Completion Evidence

~~~yaml
packet_id: V4-02-P01-profiles-registry
changed_files:
  - src/aistock_agent/brain_v4/contracts/research.py        # 追加两可选字段
  - src/aistock_agent/brain_v4/contracts/manifest.py
  - src/aistock_agent/brain_v4/controller/__init__.py
  - src/aistock_agent/brain_v4/controller/stop_policy.py
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/registry.py
  - src/aistock_agent/brain_v4/features/briefing/manifest.yaml
  - src/aistock_agent/brain_v4/features/market_trace/manifest.yaml
  - src/aistock_agent/brain_v4/generated/registry.lock
  - tests/unit/brain_v4/test_profiles.py
  - tests/unit/brain_v4/test_stop_policy.py
  - tests/unit/brain_v4/test_registry.py
  - CHANGELOG.md
tests:
  - command: uv run pytest tests/unit/brain_v4 -q
    result: 26 passed
static_checks:
  - command: uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
    result: All checks passed!
  - command: uv run mypy src/aistock_agent/brain_v4
    result: Success: no issues found in 15 source files
contract_versions: [v4.1-draft]
fixture_or_snapshot_sha: registry_sha256=433969d7a2c0ab3d17c17e479e7ff0796341f305ba67b8e3f4cd79b44e425629
generated_artifacts:
  - src/aistock_agent/brain_v4/generated/registry.lock
known_limitations:
  - PyYAML 无类型存根，`registry.py` 对 `import yaml` 使用 `# type: ignore[import-untyped]`，未新增依赖。
  - PyYAML 为既有传递依赖，未写入 pyproject（本 Packet 禁止改 pyproject）。
unresolved_questions: []
~~~

## 9. Stop Conditions

出现以下任一情况停止并请求签发人修改执行包：需改白名单外文件；契约与计划不一致
（已授权的 RoundLedger 两字段除外）；需新增依赖或环境变量；fixture 无法复现预期失败；
需放宽测试才能转绿。本 Packet 执行期间均未触发。
