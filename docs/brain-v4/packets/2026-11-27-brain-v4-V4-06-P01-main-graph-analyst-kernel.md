# Execution Packet · V4-06-P01 · 研究主图与受限分析师内核

> 本文件是已执行完毕的执行包，对应计划
> `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 6。

## 1. Packet Metadata

~~~yaml
packet_id: V4-06-P01-main-graph-analyst-kernel
objective: 组装 V4 研究主图（研究循环 + 协作路由 + 议会 + 单写研判提交），并实现有步数上限的受限分析师内核
owner: Orchestration Owner + Domain Owners
prerequisite_repo: aistock-agent-py
prerequisite_commit: db556bb
contract_baseline: v4.1-draft
feature_id: platform/research-main-graph
expected_commit_message: feat(brain-v4): 研究主图与受限分析师内核
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/graph/builder.py
  - src/aistock_agent/brain_v4/analysts/__init__.py
  - src/aistock_agent/brain_v4/analysts/kernel.py
  - src/aistock_agent/brain_v4/analysts/profiles.py
  - tests/unit/brain_v4/test_no_deprecated_agent_api.py
  - tests/integration/brain_v4/test_research_loop.py
  - CHANGELOG.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/state.py
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/registry.py
  - pyproject.toml
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

实际修改文件是上述白名单的子集；未改任何既有契约、state、profiles、controller、
deliberation、tools、events、register、`pyproject.toml`、既有测试或 app-api/前端。

## 3. Contracts

### Consumes

- `graph.profiles.RunProfile / get_profile`。
- `contracts.research.ResearchRequest / Hypothesis / CausalEdge / ResearchGap / RoundLedger`。
- `contracts.judgment.V4Judgment`。
- `contracts.deliberation.CollaborationDecision / CouncilDecision / PeerBrief / BeliefUpdate / PositionPaper`。
- `controller.planner.build_plan`、`controller.sufficiency.evaluate_sufficiency`。
- `deliberation.router.route_collaboration`、`deliberation.graph.build_council_graph`。
- `graph.state.merge_by_id / append_events / sum_usage / canonical_digest`。
- `tools.gateway.ToolGateway / ToolClient`。

### Produces

- `graph.builder.build_research_graph()`。
- `graph.builder.commit_judgment(state) -> V4Judgment`。
- `graph.builder.JudgmentEvidenceError / JudgmentGateError`。
- `analysts.kernel.AnalystResult / run_analyst(...)`。
- `analysts.profiles.analyst_mode / analyst_seats / analyst_tool_groups`。

## 4. 关键不变量

- 主图并行扇出只用 `Send`，回改与议会推进用 `Command`。
- 并行通道 `evidence_items / analyst_results / usage / errors / trace` 均挂显式
  reducer（复用 `merge_by_id / append_events / sum_usage`）；`hypothesis_graph /
  causal_graph / council_decision / judgment_draft` 等单写通道不挂 reducer。
- 研判单写校验所有引用证据存在于 `EvidenceLedger`；议会/红队提交前必须有
  R0 独立立场与 R1 `PeerBrief` + `BeliefUpdate`。
- `status` 与 `stop_reason` 独立：`status` 只由 `decision` 推导，`stop_reason`
  取运行状态，二者不互相替代。
- 所有节点只从 state 透传 `as_of / data_cutoff / snapshot_id`；研判
  `created_at = request.as_of`，不读系统当前时间。
- 分析师只产出 `AnalystResult`，不写最终 judgment 或规范图；步数上限来自
  `RunProfile.analyst_max_steps`，超限返回结构化 `stalled`。
- 模型与工具客户端由调用方注入；离线假模型/假客户端不触网、不读 API key。
- V4 包内不出现旧版 ReAct 预构建 API。

## 5. 验证结果

- 红态：`uv run pytest tests/unit/brain_v4/test_no_deprecated_agent_api.py
  tests/integration/brain_v4/test_research_loop.py -q` → 收集失败
  `ModuleNotFoundError: No module named 'aistock_agent.brain_v4.analysts'`。
- 绿态：同一命令 → 6 passed。
- `uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q` → 94 passed。
- `uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
  tests/integration/brain_v4` → All checks passed!
- `uv run mypy src/aistock_agent/brain_v4` → Success: no issues found in 39 source files。
- `git status --porcelain`：仅新增白名单文件（analysts/、graph/builder.py、两个测试）
  与 `CHANGELOG.md` 修改，无越界改动。
