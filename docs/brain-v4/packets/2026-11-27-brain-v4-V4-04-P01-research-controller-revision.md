# Execution Packet · V4-04-P01 · 研究控制器、充分性门禁与局部回改

> 本文件是**已填写并执行完毕**的执行包（由 `brain-v4-task-packet-template.md` 派生），
> 对应计划 `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 4。

## 1. Packet Metadata

~~~yaml
packet_id: V4-04-P01-research-controller-revision
objective: 确定性研究计划、结构化充分性门禁、因果影响遍历与局部回改决策（含振荡与回改预算保护）
owner: Judgment Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: ffd46a7
contract_baseline: v4.1-draft
feature_id: platform/research-controller
expected_commit_message: feat(brain-v4): 研究控制器、充分性门禁与局部回改
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/controller/planner.py
  - src/aistock_agent/brain_v4/controller/sufficiency.py
  - src/aistock_agent/brain_v4/controller/revision.py
  - src/aistock_agent/brain_v4/controller/impact.py
  - tests/unit/brain_v4/test_revision.py
  - tests/unit/brain_v4/test_sufficiency.py
  - tests/unit/brain_v4/test_planner.py
  - CHANGELOG.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/registry.py
  - pyproject.toml
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  - tests/unit/（brain_v4 子目录除外）
~~~

规则：实际修改文件必须是 `allowed_files` 的子集；未改任何既有契约、state、profiles、registry、tools、events。

## 3. Contracts

### Consumes

- `contracts.research.ResearchRequest / Hypothesis / CausalEdge / ResearchGap / RevisionEvent / RoundLedger`。
- `contracts.evidence.EvidenceItem`。
- `graph.profiles.RunProfile` 与 `get_profile`。
- `graph.state.canonical_digest`（复用既有规范化摘要，不新建工具）。

### Produces

- `controller.planner.ResearchPlan`、`EvidenceRequirement`、`build_plan`。
- `controller.sufficiency.SufficiencyReport`、`SufficiencyScores`、`evaluate_sufficiency`。
- `controller.impact.ImpactResult`、`impacted_refs`。
- `controller.revision.Contradiction`、`RevisionDecision`、`revise`、`build_revision_event`。

### Invariants

- 计划纯确定性：同 request / hypotheses / profile / round_no 得到同一 `plan_id` 与同一计划；不读系统时间。
- 门禁只产出结构化 `ResearchGap`，不写叙述文本；`as_of` / `data_cutoff` 显式入参。
- 影响遍历只返回下游 hypothesis/claim；兄弟假设与无关 claim 不被波及。
- 回改动作集合：`add_evidence | reweight | revise_hypothesis | split | merge | reject`；落 `RevisionEvent` 时 `revise_hypothesis → revise`。
- 单假设最多回改 2 次；第 3 次返回 `reject`；A→B→A 标记 `oscillation=True`。
- 所有新契约 `extra="forbid"`，无 `Any`。

## 4. Required Context

1. `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 4（第 362-407 行）。
2. `docs/智慧大脑-V4-团队接入与迁移规范.md`（ResearchController、充分性门禁、影响分析、回改）。
3. `docs/superpowers/specs/2026-11-26-smart-brain-v4-design.md` §2.8-§2.10、§3.11、§5。
4. `src/aistock_agent/brain_v4/contracts/__init__.py`、`contracts/research.py`、`contracts/evidence.py`、
   `graph/state.py`、`graph/profiles.py`、`controller/stop_policy.py`。

## 5. Failing Test First

测试文件：`tests/unit/brain_v4/test_revision.py`（计划 Step 1 回归 + 振荡/预算/影响遍历）、
`tests/unit/brain_v4/test_sufficiency.py`（七维度）、`tests/unit/brain_v4/test_planner.py`（确定性计划）。

运行与预期红（均已真实复现）：

~~~text
# revision
ModuleNotFoundError: No module named 'aistock_agent.brain_v4.controller.impact'

# sufficiency
ModuleNotFoundError: No module named 'aistock_agent.brain_v4.controller.sufficiency'

# planner
ModuleNotFoundError: No module named 'aistock_agent.brain_v4.controller.planner'
~~~

## 6. Implementation Steps

1. 先写 `test_revision.py`、`test_sufficiency.py`、`test_planner.py`，真实运行确认 ImportError 红态。
2. 实现 `impact.py` 的下游遍历与 `revision.py` 的回改判定，`test_revision.py` 7 passed。
3. 实现 `sufficiency.py` 七维度门禁与 `planner.py` 确定性计划，三文件 24 passed。
4. 全量 brain_v4 回归 60 passed，ruff/mypy 全绿。

## 7. Verification

~~~text
uv run pytest tests/unit/brain_v4/test_revision.py tests/unit/brain_v4/test_sufficiency.py tests/unit/brain_v4/test_planner.py -q
uv run pytest tests/unit/brain_v4 -q
uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
uv run mypy src/aistock_agent/brain_v4
git status --porcelain
~~~

真实结果：

- 三文件 pytest：`24 passed`。
- 全量 brain_v4：`60 passed`。
- `ruff`：`All checks passed!`。
- `mypy`：`Success: no issues found in 25 source files`。
- 红态：三处 `ModuleNotFoundError` 已真实复现（见 §5）。

## 8. Completion Evidence

~~~yaml
packet_id: V4-04-P01-research-controller-revision
changed_files:
  - src/aistock_agent/brain_v4/controller/impact.py
  - src/aistock_agent/brain_v4/controller/revision.py
  - src/aistock_agent/brain_v4/controller/sufficiency.py
  - src/aistock_agent/brain_v4/controller/planner.py
  - tests/unit/brain_v4/test_revision.py
  - tests/unit/brain_v4/test_sufficiency.py
  - tests/unit/brain_v4/test_planner.py
  - CHANGELOG.md
tests:
  - command: uv run pytest tests/unit/brain_v4/test_revision.py tests/unit/brain_v4/test_sufficiency.py tests/unit/brain_v4/test_planner.py -q
    result: 24 passed
  - command: uv run pytest tests/unit/brain_v4 -q
    result: 60 passed
static_checks:
  - command: uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
    result: All checks passed!
  - command: uv run mypy src/aistock_agent/brain_v4
    result: Success: no issues found in 25 source files
contract_versions: [v4.1-draft]
known_limitations:
  - `ResearchPlan` / `SufficiencyReport` / `RevisionDecision` 为本 Packet 新产出的控制器契约，尚未进入公共 JSON Schema 生成流程。
  - 影响遍历按假设 id 主干匹配版本，避免每次回改重建因果图；若未来因果边需要按版本区分，需另开平台契约包。
unresolved_questions: []
~~~

## 9. Stop Conditions

出现以下任一情况停止并请求签发人修改执行包：需改白名单外文件；既有契约字段无法满足且必须改契约；
需新增依赖或环境变量；测试需放宽才能通过。本 Packet 执行期间均未触发。
