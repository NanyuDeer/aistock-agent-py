# Execution Packet · V4-01-P01 · V4 公共契约与确定性 State 基座

> 本文件是**已填写的执行包**（由 `brain-v4-task-packet-template.md` 派生），不是模板。
> 执行者只按本文件工作；不得读取总计划后自行选择其他 Task。

## 0. Packet 状态

~~~yaml
state: 待签发确认 / prerequisite 未满足
blocker: |
  aistock-agent-py 工作树存在 Task 0 未提交变更（28 个文件：graph/brain 删除、
  routes.py/config.py 修改、brain 相关测试删除等）。Task 1 必须建立在 Task 0
  的单独 commit 之上，否则本 Packet 的范围检查会混入 28 个无关文件。
  处理方式见 §9 前置条件。
~~~

## 1. Packet Metadata

~~~yaml
packet_id: V4-01-P01
objective: 建立 brain_v4 公共契约模型与确定性 state reducer 基座，使后续 Gateway / Controller / 议会节点可以在同一套不可变契约上开发
owner: Contract Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: <填 Task 0 提交后的 40 位 SHA；当前基线 dfdba021706ceda8d02f79a0c65b26b353880d30 不可直接用，见 §9>
contract_baseline: v4.0-draft
feature_id: platform/contracts
expected_commit_message: feat(brain-v4): 公共契约与确定性 state 基座
~~~

## 2. Scope Lock

~~~yaml
allowed_files:
  # 包骨架（Python 包必须存在 __init__.py 才能被 setuptools 与 pytest 识别）
  - src/aistock_agent/brain_v4/__init__.py
  - src/aistock_agent/brain_v4/contracts/__init__.py
  - src/aistock_agent/brain_v4/graph/__init__.py
  # 契约
  - src/aistock_agent/brain_v4/contracts/research.py
  - src/aistock_agent/brain_v4/contracts/evidence.py
  - src/aistock_agent/brain_v4/contracts/judgment.py
  - src/aistock_agent/brain_v4/contracts/deliberation.py
  - src/aistock_agent/brain_v4/contracts/briefing.py
  - src/aistock_agent/brain_v4/contracts/events.py
  # 确定性 state
  - src/aistock_agent/brain_v4/graph/state.py
  # 测试
  - tests/unit/brain_v4/__init__.py
  - tests/unit/brain_v4/test_contracts.py
  - tests/unit/brain_v4/test_state_reducers.py
  # 签发人授权的范围修订（2026-11-27，用户确认 A）：补 CHANGELOG 记录变更
  - CHANGELOG.md
max_files_changed: 14

forbidden_paths:
  # 本 Packet 之后才允许创建的公共层
  - src/aistock_agent/brain_v4/graph/builder.py
  - src/aistock_agent/brain_v4/graph/profiles.py
  - src/aistock_agent/brain_v4/controller/
  - src/aistock_agent/brain_v4/tools/
  - src/aistock_agent/brain_v4/deliberation/
  - src/aistock_agent/brain_v4/analysts/
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/brain_v4/renderers/
  - src/aistock_agent/brain_v4/verification/
  - src/aistock_agent/brain_v4/events/
  - src/aistock_agent/brain_v4/learning/
  - src/aistock_agent/brain_v4/generated/
  - brain_v4_evals/
  # 既有生产代码一律不动
  - src/aistock_agent/graph/brain/
  - src/aistock_agent/api/
  - src/aistock_agent/config.py
  - src/aistock_agent/schemas/
  - src/aistock_agent/services/
  - src/aistock_agent/agents/
  - src/aistock_agent/prompts/
  # 其他仓库
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  # 既有测试
  - tests/unit/ (brain_v4 子目录除外)
  - tests/integration/
  - tests/e2e/
  - tests/fixtures/
~~~

**规则**：实际 `git diff --name-only` 必须是 `allowed_files` 的子集。需要新增文件时停止并请求签发人更新本 Packet，不得自行扩大范围。

## 3. Contracts

### Consumes

- 无（本 Packet 是 V4 契约的创建起点）。只消费 PyPI 既有依赖 `pydantic>=2.11`，不得新增依赖。

### Produces

全部模型均 `model_config = ConfigDict(extra="forbid")`，`as_of` / `snapshot_id` / `run_id` 为必填，禁止任何字段默认取系统当前时间。

#### `contracts/research.py`
| 模型 | 字段（严格照抄） | 来源 |
|---|---|---|
| `ResearchRequest` | `request_id, run_id, profile, subject_type, subject_code, research_goal, required_questions: list[str], horizon, as_of: datetime, data_cutoff: datetime, snapshot_id, config_version, model_version, prompt_version, method_version, budget_policy: str, stop_policy: str` | spec §3.1 + 接入规范 §5.5 |
| `Hypothesis` | `hypothesis_id, version: int(ge=1), statement, status: Literal["open","supported","weakened","rejected","frozen"], prior_confidence: float, assumptions: list[str], disconfirmers: list[str](min_length=1), required_evidence_types: list[str], parent_ids: list[str], scope: str`；`@property ref -> f"{hypothesis_id}:v{version}"` | spec §3.2 + plan Task 1 |
| `CausalEdge` | `edge_id, from_node, to_node, mechanism, supporting_evidence_ids: list[str], contradicting_evidence_ids: list[str], edge_type: Literal["fact","inference","hypothesis"], confidence: float, valid_time: datetime, magnitude_check: str, counterfactual_check: str` | spec §3.5 |
| `ResearchGap` | `gap_id, run_id, issue_id, gap_type: Literal["missing_evidence","time_order","mechanism","magnitude","alternative","invalidator","edge_support"], description, required_evidence_types: list[str], target_hypothesis_refs: list[str], materiality: Literal["low","medium","high"], status: Literal["open","researching","resolved","abandoned"]` | spec §2.4 提及 + plan Task 4 Step 4 六项检查 |
| `RevisionEvent` | `revision_id, run_id, action: Literal["add_evidence","reweight","revise","split","merge","reject"], target_refs: list[str], triggering_evidence_ids: list[str], impacted_hypothesis_refs: list[str], impacted_claim_ids: list[str], reason: str, case_version: int, created_at: datetime` | spec §5.2 + §3.11 + Task 4 回归用例 |
| `RoundLedger` | `round_no: int(ge=0), new_evidence_ids: list[str], changed_hypothesis_ids: list[str], unresolved_gaps: list[str], contradictions: list[str], next_action: str, progress_score: float, stall_count: int(ge=0), revision_budget_left: int(ge=0), stop_reason: str | None` | spec §3.6 |

#### `contracts/evidence.py`
| 模型 | 字段 | 来源 |
|---|---|---|
| `EvidenceItem` | `evidence_id, source, source_type, event_time: datetime, published_at: datetime, fetched_at: datetime, as_of: datetime, evidence_kind: Literal["direct","indirect","model_inference"], supports_hypothesis_refs: list[str], contradicts_hypothesis_refs: list[str], data_quality: Literal["high","medium","low"], time_confidence: Literal["high","medium","low"], duplicate_of: str | None, conflicts_with: list[str], tool: str, tool_params: dict[str, str | int | float | bool], response_digest: str, snapshot_id: str` | spec §3.3 |
| `EvidenceLedger` | `run_id, snapshot_id, as_of: datetime, items: list[EvidenceItem], conflict_ids: list[str]` | spec §3.3 |

#### `contracts/judgment.py`
| 模型 | 字段 | 来源 |
|---|---|---|
| `V4Judgment` | `judgment_id, run_id, profile, subject_type, subject_code, horizon, as_of: datetime, data_cutoff: datetime, snapshot_id, decision: Literal["accept","revise","insufficient","escalate"], status: Literal["ok","partial","insufficient"], stop_reason: str, selected_hypothesis_refs: list[str], rejected_hypothesis_refs: list[str], minority_opinions: list[str], invalidators: list[str], evidence_ids: list[str], unresolved_question_ids: list[str], confidence: float, parent_judgment_ids: list[str], relation: str | None, config_version, model_version, prompt_version, method_version, created_at: datetime` | spec §3.12 + §2.12 + 接入规范 §5.5-5.6 |

#### `contracts/deliberation.py`
| 模型 | 字段 | 来源 |
|---|---|---|
| `CaseFile` | `case_id, run_id, profile, case_version: int(ge=1), question, decision_type, materiality: Literal["low","medium","high"], subject, horizon, as_of: datetime, snapshot_id, frozen_fact_ids: list[str], hypothesis_refs: list[str], open_issue_ids: list[str], deadline: datetime, budget_policy: str, decision_rubric_version` | spec §3.7 |
| `AgendaItem` | `issue_id, case_id, question, candidate_hypothesis_refs: list[str], decision_criteria: list[str], materiality, owner_role, status: Literal["open","researching","deliberating","resolved","unresolved","escalated"], opened_at: datetime, resolved_at: datetime | None, unresolved_reason: str | None` | spec §3.8 |
| `PositionPaper` | `position_id, issue_id, analyst_role, hypothesis_ref, thesis, mechanism_edge_ids: list[str], evidence_ids: list[str], assumptions: list[str], confidence: float, disconfirmers: list[str](min_length=1), unknowns: list[str], requested_checks: list[str], created_at: datetime` | spec §3.9 |
| `Objection` | `objection_id, issue_id, raised_by, target_type: Literal["position","claim","edge","evidence"], target_id, objection_type: Literal["chronology","provenance","mechanism","magnitude","alternative","leakage","scope"], severity: Literal["low","medium","high"], argument, required_evidence: list[str], kill_criterion: str, status: Literal["open","answered","accepted","unresolved"]` | spec §3.10 |
| `Rebuttal` | `rebuttal_id, objection_id, answered_by, response: Literal["accept","partially_accept","reject_with_evidence","request_more_evidence"], evidence_ids: list[str], revision_proposal_id: str | None, residual_uncertainty: str` | spec §3.10 |
| `RevisionProposal` | `proposal_id, issue_id, proposer, target_refs: list[str], action: Literal["add_evidence","reweight","revise","split","merge","reject"], reason, triggering_evidence_ids: list[str], expected_impact_refs: list[str], status: Literal["proposed","accepted","rejected"]` | spec §3.11 |
| `CouncilDecision` | `decision_id, issue_id, rubric_version, selected_hypothesis_refs: list[str], rejected_hypothesis_refs: list[str], criterion_scores: dict[str, float], reasoning_summary, resolved_objection_ids: list[str], unresolved_objection_ids: list[str], minority_opinions: list[str], invalidators: list[str], decision: Literal["accept","revise","insufficient","escalate"], confidence: float, created_at: datetime` | spec §3.12 |
| `ProgressLedger` | `step_no: int(ge=0), phase, agenda_status, has_progress: bool, new_evidence_count: int(ge=0), changed_ranking: bool, open_high_objection_ids: list[str], next_role: str | None, next_instruction: str | None, stall_count: int(ge=0), plan_reset_count: int(ge=0), budget_remaining: int(ge=0), stop_reason: str | None` | spec §3.13 |
| `CollaborationDecision` | `mode: Literal["SOLO_DEEP","LEAD_WITH_CONSULTANTS","ANALYST_COUNCIL","MANDATORY_RED_TEAM"], suggested_mode: str | None, trigger_codes: list[str], selected_roles: list[str], lead_role, judge_role, max_objection_rounds: int(ge=0), max_rebuttal_rounds: int(ge=0), human_required: bool` | spec §3.14 |
| `PeerBrief` | `peer_brief_id, case_id, case_version: int(ge=1), deliberation_round: int(ge=0), target_role, source_artifact_ids: list[str], competing_claims: list[str], evidence_ids: list[str], material_disagreements: list[str], open_questions: list[str], omitted_sections: list[str], generated_at: datetime` | spec §3.15 |
| `BeliefUpdate` | `belief_update_id, issue_id, analyst_role, case_version: int(ge=1), deliberation_round: int(ge=0), prior_hypothesis_ref, prior_confidence: float, action: Literal["hold","strengthen","weaken","switch","split","merge"], posterior_hypothesis_refs: list[str], posterior_confidence: list[float], influencing_artifact_ids: list[str], evidence_ids: list[str], changed_assumptions: list[str], requested_gap_ids: list[str], reason_summary` | spec §3.16 |
| `InfluenceEdge` | `influence_edge_id, source_artifact_id, target_belief_update_id, target_hypothesis_ref, influence_type: Literal["search_direction","assumption","mechanism","confidence","hypothesis_topology","objection"], effect: Literal["reinforce","weaken","redirect","split","merge","reject"], evidence_backed: bool, materiality: Literal["low","medium","high"], created_at: datetime` | spec §3.17 |
| `DeliberationRoundLedger` | `deliberation_round: int(ge=0), case_version: int(ge=1), active_issue_ids: list[str], peer_brief_ids: list[str], belief_update_ids: list[str], influence_edge_ids: list[str], new_evidence_count: int(ge=0), closed_high_objection_ids: list[str], max_belief_delta: float, ranking_changed: bool, topology_changed: bool, affected_role_ids: list[str], next_action: str, stop_reason: str | None` | spec §3.18 |

#### `contracts/briefing.py`
| 模型 | 字段 | 来源 |
|---|---|---|
| `Scenario` | `scenario_id, label, trigger_conditions: list[str], expected_path, invalidator` | spec §2.12「三情景」 |
| `MorningBody` | `overnight_changes: list[str], today_baseline: str, key_event_ids: list[str], style_sector_hypotheses: list[str], scenarios: list[Scenario](min_length=1), invalidators: list[str](min_length=1)` | spec §2.12 + Task 9 Step 1 |
| `MiddayBody` | `confirmed_hypothesis_refs: list[str], falsified_hypothesis_refs: list[str], new_drivers: list[str], afternoon_opportunities: list[str], afternoon_risks: list[str]` | spec §2.12 + Task 9 Step 2 |
| `EveningBody` | `closing_attribution_ref: str, market_trace_judgment_id: str, prediction_scorecard: list[str], errors_and_unresolved: list[str], next_day_verification: list[str]` | spec §2.12 + Task 9 Step 3 |
| `BriefingEnvelope` | `briefing_id, trading_date: date, profile: Literal["briefing_morning","briefing_midday","briefing_evening"], as_of: datetime, data_cutoff: datetime, run_id, parent_run_id: str | None, parent_judgment_ids: list[str], relation: Literal["confirm","weaken","revise","reverse","explain","verify","carry_forward"] | None, headline, summary, evidence_ids: list[str], risk_flags: list[str], body: MorningBody | MiddayBody | EveningBody, render_targets: list[str], schema_version` | spec §2.12 |

**硬校验（模型级，非引擎级）**：
- `profile == "briefing_midday"` 时 `parent_judgment_ids` 不得为空，否则 `ValidationError`（plan Task 1 测试 3）。
- `MorningBody.scenarios`、`MorningBody.invalidators`、`PositionPaper.disconfirmers`、`Hypothesis.disconfirmers` 至少 1 条（可证伪性硬约束）。

#### `contracts/events.py`
| 模型 | 字段 | 来源 |
|---|---|---|
| `EventObservation` | `observation_id, event_id, observation_type: Literal["news","announcement","research","calendar","crawl"], source, source_id, title, published_at: datetime, event_time: datetime, fetched_at: datetime, as_of: datetime, url: str | None, time_confidence: Literal["high","medium","low"], lifecycle_version: int(ge=1)` | spec §3.4 结构图 |
| `CanonicalEventRecord` | `event_id, canonical_type, canonical_title, event_time: datetime, first_seen_at: datetime, last_updated_at: datetime, valid_time: datetime, status: Literal["scheduled","active","occurred","corrected","cancelled","expired"], source_refs: list[str], observation_ids: list[str], entity_refs: list[str], dedupe_fingerprint, parent_event_id: str | None, supersedes_event_id: str | None, time_confidence: Literal["high","medium","low"], source_conflicts: list[str], lifecycle_version: int(ge=1)` | spec §3.4 |

#### `graph/state.py`
| 名称 | 内容 |
|---|---|
| `BrainV4State` | `TypedDict(total=False)`，字段：`request, case_file, evidence_ledger, hypotheses: list[Hypothesis], hypothesis_graph: dict[str, Hypothesis], causal_graph: list[CausalEdge], round_ledgers: list[RoundLedger], research_gaps: list[ResearchGap], revision_events: list[RevisionEvent], submissions: list[PositionPaper], objections: list[Objection], rebuttals: list[Rebuttal], peer_briefs: list[PeerBrief], belief_updates: list[BeliefUpdate], influence_edges: list[InfluenceEdge], revision_proposals: list[RevisionProposal], council_decision, collaboration_decision, progress_ledger, judgment_draft, usage: list[dict[str, float | str]], errors: list[dict[str, str]], conflicts: list[dict[str, str]], run_status, stop_reason, trace` |
| `PARALLEL_APPEND_CHANNELS` | `frozenset({"submissions","objections","belief_updates","influence_edges","usage","errors"})` |
| `SINGLE_WRITER_CHANNELS` | `frozenset({"hypothesis_graph","causal_graph","council_decision","judgment_draft"})` |
| `canonical_digest(payload) -> str` | 对 `json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",",":"))` 取 `sha256` 十六进制 |
| `merge_by_id(current, incoming, id_field) -> MergeResult` | 按 `id_field` 合并；同 ID 同内容去重；同 ID 不同内容时保留 `canonical_digest` **较小**者（顺序无关），并输出 `MergeConflict(record_id, kept_digest, dropped_digests)`；结果按 `id_field` 升序稳定排序 |
| `append_events(current, incoming, id_field)` | 纯追加通道，按 ID 去重后稳定排序，不覆盖 |
| `sum_usage(current, incoming) -> list[dict]` | 按 `key` 数值相加 |
| `append_errors(current, incoming)` | 分类追加，不覆盖成功结果 |

### Invariants

1. 所有契约 `extra="forbid"`；未知字段必须报错（Task 7 评测信封测试依赖此行为）。
2. 任何模型不得有 `default=now()` / `default_factory=datetime.now`。`created_at` 等时间由调用方显式传入。
3. `MergeResult` / `MergeConflict` 的数据形态必须满足「两个合并顺序产出 `model_dump()` 逐字节相等」。
4. 单写通道**不提供** reducer；`state.py` 只声明集合常量，供 Task 6 builder 拒绝并行写。
5. 本 Packet 不生成 JSON Schema、不改 app-api schema、不创建 `registry.lock`。

## 4. Required Context

只允许读取：

1. `docs/superpowers/specs/2026-11-26-smart-brain-v4-design.md` 第 304-328 行（§2.8）、369-400 行（§2.12）、402-653 行（§3.1-§3.18）
2. `docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md` 第 134-224 行（Task 1）
3. `docs/智慧大脑-V4-团队接入与迁移规范.md` §5（契约规则）
4. 最近似既有模式：`src/aistock_agent/schemas/chat_contract.py` 前 70 行（Pydantic + `ConfigDict(extra="forbid")` 写法与中文 docstring 风格）
5. `pyproject.toml`（pytest `asyncio_mode=strict`、ruff `line-length=100` 且 `ignore=["E501"]`、mypy `strict=true`、`mypy_path=src`）
6. `tests/unit/__init__.py` 存在（`tests/unit/brain_v4/` 必须配套 `__init__.py`）

**禁止**遍历仓库其他目录。

## 5. Failing Test First

### 5.1 `tests/unit/brain_v4/test_contracts.py`

~~~python
import pytest
from pydantic import ValidationError

from aistock_agent.brain_v4.contracts.briefing import BriefingEnvelope
from aistock_agent.brain_v4.contracts.events import CanonicalEventRecord, EventObservation
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest


def test_request_requires_as_of_and_snapshot():
    with pytest.raises(ValidationError) as exc:
        ResearchRequest(
            request_id="req-1",
            run_id="run-1",
            profile="market_trace",
            subject_type="market",
            subject_code="000001-SH",
            research_goal="解释今日下跌",
            horizon="1d",
        )
    message = str(exc.value)
    assert "as_of" in message
    assert "snapshot_id" in message


def test_hypothesis_revision_is_versioned():
    hypothesis = Hypothesis(
        hypothesis_id="hyp_liquidity",
        version=2,
        statement="流动性是放大器",
        status="weakened",
        prior_confidence=0.6,
        disconfirmers=["成长与防御同步下跌"],
        parent_ids=["hyp_liquidity:v1"],
        scope="market",
    )
    assert hypothesis.ref == "hyp_liquidity:v2"


def test_hypothesis_requires_disconfirmer():
    with pytest.raises(ValidationError):
        Hypothesis(
            hypothesis_id="hyp_x",
            version=1,
            statement="无失效条件的假设",
            status="open",
            prior_confidence=0.5,
            disconfirmers=[],
            scope="market",
        )


def test_contract_forbids_unknown_field():
    with pytest.raises(ValidationError):
        Hypothesis(
            hypothesis_id="hyp_y",
            version=1,
            statement="s",
            status="open",
            prior_confidence=0.5,
            disconfirmers=["d"],
            scope="market",
            unexpected_field=1,
        )


def test_midday_briefing_requires_parent_judgment():
    with pytest.raises(ValidationError):
        BriefingEnvelope(
            briefing_id="b-1",
            trading_date="2026-11-26",
            profile="briefing_midday",
            as_of="2026-11-26T11:35:00+08:00",
            data_cutoff="2026-11-26T11:30:00+08:00",
            run_id="run-mid",
            parent_judgment_ids=[],
            headline="午间",
            summary="汇总",
            body={"confirmed_hypothesis_refs": [], "falsified_hypothesis_refs": [],
                  "new_drivers": [], "afternoon_opportunities": [], "afternoon_risks": []},
            render_targets=["app"],
            schema_version="1.0",
        )


def test_event_record_separates_event_from_observations():
    observations = [
        EventObservation(
            observation_id=f"obs-{i}",
            event_id="ev-1",
            observation_type=kind,
            source=src,
            source_id=f"{src}-1",
            title="同一件事",
            published_at="2026-11-26T09:00:00+08:00",
            event_time="2026-11-26T08:50:00+08:00",
            fetched_at="2026-11-26T09:01:00+08:00",
            as_of="2026-11-26T10:00:00+08:00",
            time_confidence="high",
            lifecycle_version=1,
        )
        for i, (kind, src) in enumerate(
            [("news", "jin10"), ("announcement", "cninfo"), ("calendar", "tushare")]
        )
    ]
    event = CanonicalEventRecord(
        event_id="ev-1",
        canonical_type="policy",
        canonical_title="同一件事",
        event_time="2026-11-26T08:50:00+08:00",
        first_seen_at="2026-11-26T09:00:00+08:00",
        last_updated_at="2026-11-26T09:01:00+08:00",
        valid_time="2026-11-26T08:50:00+08:00",
        status="occurred",
        observation_ids=[o.observation_id for o in observations],
        dedupe_fingerprint="fp-1",
        time_confidence="high",
        lifecycle_version=1,
    )
    assert event.event_id == "ev-1"
    assert len(event.observation_ids) == 3
    assert not hasattr(event, "beneficiary_sector")
    assert not hasattr(event, "impact")
~~~

### 5.2 `tests/unit/brain_v4/test_state_reducers.py`

~~~python
from aistock_agent.brain_v4.contracts.research import Hypothesis
from aistock_agent.brain_v4.graph.state import (
    PARALLEL_APPEND_CHANNELS,
    SINGLE_WRITER_CHANNELS,
    append_events,
    merge_by_id,
    sum_usage,
)


def _hyp(hid: str, statement: str, version: int = 1) -> dict:
    return Hypothesis(
        hypothesis_id=hid,
        version=version,
        statement=statement,
        status="open",
        prior_confidence=0.5,
        disconfirmers=["d"],
        scope="market",
    ).model_dump()


def test_merge_is_arrival_order_independent():
    a = _hyp("hyp_a", "A")
    b = _hyp("hyp_b", "B")
    left = merge_by_id([], [a, b], "hypothesis_id")
    right = merge_by_id([], [b, a], "hypothesis_id")
    assert left.model_dump() == right.model_dump()
    assert [r["hypothesis_id"] for r in left.items] == ["hyp_a", "hyp_b"]


def test_merge_detects_conflict_without_last_writer_wins():
    v1 = _hyp("hyp_a", "旧表述")
    v2 = _hyp("hyp_a", "新表述")
    forward = merge_by_id([], [v1, v2], "hypothesis_id")
    backward = merge_by_id([], [v2, v1], "hypothesis_id")
    assert forward.items == backward.items
    assert forward.conflicts == backward.conflicts
    assert len(forward.conflicts) == 1
    assert forward.conflicts[0].record_id == "hyp_a"


def test_append_channel_dedupes_by_id():
    first = [{"id": "x", "v": 1}]
    second = [{"id": "x", "v": 1}, {"id": "y", "v": 2}]
    assert append_events(first, second, "id") == [{"id": "x", "v": 1}, {"id": "y", "v": 2}]


def test_usage_sums_numeric_keys():
    assert sum_usage([{"key": "token", "value": 10}], [{"key": "token", "value": 5}]) == [
        {"key": "token", "value": 15}
    ]


def test_channel_partition_is_disjoint():
    assert not (PARALLEL_APPEND_CHANNELS & SINGLE_WRITER_CHANNELS)
    assert "hypothesis_graph" in SINGLE_WRITER_CHANNELS
    assert "judgment_draft" in SINGLE_WRITER_CHANNELS
    assert "submissions" in PARALLEL_APPEND_CHANNELS
~~~

### Run

~~~text
uv run pytest tests/unit/brain_v4/test_contracts.py tests/unit/brain_v4/test_state_reducers.py -q
~~~

### Expected failure before implementation

`ModuleNotFoundError: No module named 'aistock_agent.brain_v4'`（collection error）。
若失败原因不是「模块不存在」而是其他（例如 pytest 配置导致无法收集），停止并报告。

## 6. Implementation Steps

每步只做一件事，完成后进入下一步：

1. 创建 `src/aistock_agent/brain_v4/__init__.py`、`contracts/__init__.py`、`graph/__init__.py`（仅中文模块 docstring，无逻辑）。
2. 创建 `tests/unit/brain_v4/__init__.py`（空文件）。
3. 写 §5.1 `test_contracts.py`，运行，确认红。
4. 在 `contracts/research.py` 定义 §3 表中 `ResearchRequest / Hypothesis / CausalEdge / ResearchGap / RevisionEvent / RoundLedger`。
5. 在 `contracts/evidence.py` 定义 `EvidenceItem / EvidenceLedger`。
6. 在 `contracts/judgment.py` 定义 `V4Judgment`。
7. 在 `contracts/deliberation.py` 定义 13 个议事模型。
8. 在 `contracts/briefing.py` 定义 `Scenario / MorningBody / MiddayBody / EveningBody / BriefingEnvelope`，并加 `model_validator` 实现「midday 必须有父判断」。
9. 在 `contracts/events.py` 定义 `EventObservation / CanonicalEventRecord`。
10. 运行 §5 测试至全绿。
11. 写 §5.2 `test_state_reducers.py`，先确认因 `state.py` 不存在而红，再在 `graph/state.py` 实现 §3 表中 8 个符号。
12. 运行两个测试文件 + ruff + mypy，修正本 Packet 范围内的失败。
13. 运行 `git diff --name-only` 做范围检查并记录。

**禁止事项**

- 不修改 allowlist 外任何文件；不新增依赖、环境变量、数据库字段。
- 不创建 `builder.py`、`profiles.py`、`registry.py`、`gateway.py`、`deliberation/` 实现——它们属于后续 Packet。
- 不生成 JSON Schema，不写 TypeScript 类型。
- 不为通过测试而放宽 `extra="forbid"`、删除 `min_length=1` 或跳过断言。
- 不实现任何 LLM 调用、不 import `langgraph` / `langchain`。
- 不顺手格式化或重构既有文件。

## 7. Verification

按顺序执行，任何一步失败都不得提交：

~~~text
uv run pytest tests/unit/brain_v4/test_contracts.py tests/unit/brain_v4/test_state_reducers.py -q
uv run pytest tests/unit/brain_v4 -q
uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
uv run mypy src/aistock_agent/brain_v4
git diff --name-only
git status --porcelain
~~~

范围检查（本 Packet 未产出 `verify_change_scope.py` 前的手工替代）：

~~~powershell
$allowed = @(
  'src/aistock_agent/brain_v4/__init__.py',
  'src/aistock_agent/brain_v4/contracts/__init__.py',
  'src/aistock_agent/brain_v4/graph/__init__.py',
  'src/aistock_agent/brain_v4/contracts/research.py',
  'src/aistock_agent/brain_v4/contracts/evidence.py',
  'src/aistock_agent/brain_v4/contracts/judgment.py',
  'src/aistock_agent/brain_v4/contracts/deliberation.py',
  'src/aistock_agent/brain_v4/contracts/briefing.py',
  'src/aistock_agent/brain_v4/contracts/events.py',
  'src/aistock_agent/brain_v4/graph/state.py',
  'tests/unit/brain_v4/__init__.py',
  'tests/unit/brain_v4/test_contracts.py',
  'tests/unit/brain_v4/test_state_reducers.py'
)
git status --porcelain | ForEach-Object { ($_ -replace '^\s*\S+\s+','') } |
  Where-Object { $_ -notin $allowed } | ForEach-Object { "OUT OF SCOPE: $_" }
~~~

预期：只输出 13 个允许文件（且**不得**出现 Task 0 的 28 个文件——见 §9）。

## 8. Completion Evidence

执行者只提交以下证据，不得宣称未运行的检查通过：

~~~yaml
packet_id: V4-01-P01
changed_files: []
tests:
  - command: uv run pytest tests/unit/brain_v4 -q
    result: <真实计数，如 11 passed>
  - command: uv run pytest tests/unit -q
    result: <真实计数，与 Task 0 基线 5106 passed / 5 skipped 对比>
static_checks:
  - command: uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4
    result: <pass/fail>
  - command: uv run mypy src/aistock_agent/brain_v4
    result: <pass/fail>
contract_versions: [v4.0-draft]
fixture_or_snapshot_sha: not-applicable
scope_check:
  command: git status --porcelain 过滤
  result: <超范围文件列表，应为空>
generated_artifacts: []
known_limitations:
  - ResearchGap / EventObservation / Scenario 的字段在 spec 中无逐字段清单，已按 §2.4 / §3.4 / §2.12 的语义最小定义
unresolved_questions: []
~~~

## 9. Stop Conditions

出现任一情况必须停止并报告，不得猜测：

1. 需要修改 `allowed_files` 以外文件。
2. `ResearchRequest` / `Hypothesis` 等模型签名与 §3 表不一致。
3. 测试失败原因不是「模块不存在」（Step 2 预期红）。
4. 需要新增依赖、环境变量、路由或数据库字段。
5. 发现工作树存在与本 Packet 冲突的他人未提交修改。
6. **Task 0 变更仍未提交**——此时不得开始 Task 1（见下）。

### 前置条件（必须由用户先决定）

`aistock-agent-py` 当前 `git status` 显示 28 个 Task 0 未提交变更。Task 1 开始前必须在以下两项中选择一项：

- **方案 A（推荐）**：先把 Task 0 变更单独提交并推送，取得新的 40 位 SHA 填入 `prerequisite_commit`，Task 1 在此基础上提交，范围检查才干净。
- **方案 B**：明确接受 Task 1 的范围检查以「Task 0 的 28 个文件 + 本 Packet 13 个文件」为已知偏差，并在完成证据中显式列出该偏差；不推荐，会破坏「一个 Packet 一个提交」的门禁。

## 10. Model Invocation

> 严格执行本 Packet。先验证 prerequisite commit 与 scope，再写指定失败测试。只修改 allowed_files，不做额外设计或重构。任何 Stop Condition 出现时停止并报告，不自行扩大范围。完成后返回 Completion Evidence，不宣布未运行的检查通过。
