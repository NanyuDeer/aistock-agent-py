# Execution Packet · V4-05-P01 · 案卷制分析师议会与协作路由

> 本文件是已执行完毕的执行包，对应计划
> `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` Task 5。

## 1. Packet Metadata

~~~yaml
packet_id: V4-05-P01-casefile-council
objective: 确定性协作路由、案卷制议会多轮图（R0 独立立论 → PeerBrief → 交叉质询 → BeliefUpdate → 收敛门禁）、单写提交与裁判不变量
owner: Deliberation/Judgment Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: c1ee0d5
contract_baseline: v4.1-draft
feature_id: platform/deliberation
expected_commit_message: feat(brain-v4): 案卷制分析师议会与协作路由
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/deliberation/__init__.py
  - src/aistock_agent/brain_v4/deliberation/router.py
  - src/aistock_agent/brain_v4/deliberation/agenda.py
  - src/aistock_agent/brain_v4/deliberation/roles.py
  - src/aistock_agent/brain_v4/deliberation/peer_briefs.py
  - src/aistock_agent/brain_v4/deliberation/beliefs.py
  - src/aistock_agent/brain_v4/deliberation/convergence.py
  - src/aistock_agent/brain_v4/deliberation/graph.py
  - src/aistock_agent/brain_v4/deliberation/judge.py
  - src/aistock_agent/brain_v4/deliberation/committer.py
  - tests/unit/brain_v4/test_collaboration_router.py
  - tests/unit/brain_v4/test_deliberation_reducers.py
  - tests/unit/brain_v4/test_belief_updates.py
  - tests/integration/brain_v4/__init__.py
  - tests/integration/brain_v4/test_analyst_council.py
  - CHANGELOG.md
forbidden_paths:
  - src/aistock_agent/brain_v4/contracts/
  - src/aistock_agent/brain_v4/graph/state.py
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/registry.py
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/events/
  - pyproject.toml
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
~~~

实际修改文件是上述白名单的子集；未改任何既有契约、`graph/state.py`、profiles、
registry、controller、tools、events、既有测试或 `pyproject.toml`。

## 3. Contracts

### Consumes

- `contracts.deliberation` 全部议事契约（`CaseFile`、`AgendaItem`、`PositionPaper`、
  `Objection`、`Rebuttal`、`RevisionProposal`、`CouncilDecision`、`ProgressLedger`、
  `CollaborationDecision`、`PeerBrief`、`BeliefUpdate`、`InfluenceEdge`、
  `DeliberationRoundLedger`）。
- `contracts.research`、`contracts.evidence`。
- `controller.sufficiency.SufficiencyReport`（路由/收敛只读结构化信号）。
- `graph.state.merge_by_id / canonical_digest / sum_usage` 与 `graph.profiles.get_profile`。

### Produces

- `deliberation.router.route_collaboration`。
- `deliberation.agenda.build_agenda / merge_submissions`。
- `deliberation.roles.build_role_context / run_role_node / select_roles`。
- `deliberation.peer_briefs.build_peer_briefs`。
- `deliberation.beliefs.validate_belief_update / belief_update_from_peer_brief / make_hold_update`。
- `deliberation.convergence.evaluate_convergence / validate_objection`。
- `deliberation.committer.RevisionCommitter`。
- `deliberation.judge.CouncilJudge`。
- `deliberation.graph.build_council_graph`。

## 4. 关键不变量

- 路由只读显式特征；模型 `suggested_mode` 不覆盖确定性 `mode`；`alert_hot` 恒为
  `SOLO_DEEP` 且 `selected_roles == []`。
- R0 不暴露任何同伴摘要；R1+ 才生成 role-scoped `PeerBrief`，且不得进入
  `EvidenceLedger`。
- 没有新增证据或机制修正时，置信度不得上调；hold 也必须落 `BeliefUpdate`。
- 异议缺 `target_id` / `severity` / `kill_criterion` 一律拒绝。
- 无新证据、无高严重度未解异议、无拓扑变化、无实质信念变化时标记 `no_progress`。
- 只有 `RevisionCommitter.commit` 能改规范假设/因果图。
- 裁判只产出 `CouncilDecision`；引用冻结账本外证据直接拒绝；保留少数意见。

## 5. 验证结果

- 红态：四个新测试文件均因 `ModuleNotFoundError: No module named
  'aistock_agent.brain_v4.deliberation'` 在收集阶段失败。
- 绿态：`uv run pytest tests/unit/brain_v4/test_collaboration_router.py
  tests/unit/brain_v4/test_deliberation_reducers.py
  tests/unit/brain_v4/test_belief_updates.py
  tests/integration/brain_v4/test_analyst_council.py -q` → 28 passed。
- `uv run pytest tests/unit/brain_v4 -q` → 87 passed。
- `uv run pytest tests/integration/brain_v4 -q` → 1 passed。
- `uv run ruff check ...` → All checks passed!
- `uv run mypy src/aistock_agent/brain_v4` → Success: no issues found in 35 source files。
