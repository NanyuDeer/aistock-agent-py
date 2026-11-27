# 智慧大脑 V4 两条业务链试点实施计划

> V4.4 更新：2026-11-27

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 删除未上线的错误 `brain` 原型，在不影响当前真实生产功能链的前提下建立 V4 研究内核，并用“早午晚报日内链 + 市场溯源”完成影子试点。晚报复用市场溯源，不允许形成第二套盘后归因逻辑。

**Architecture:** V4 使用独立 <code>brain_v4</code> 包，不导入或兼容 <code>graph/brain</code>，也不创建 <code>brain_v3</code>。组长先完成公共契约、状态、证据网关、控制器、LangGraph 主图、案卷制议会、影子归档和评测基础设施，并用合成案卷验收；随后接入市场溯源和早午晚报两个试点校正抽象，冻结 V4.1 接口后再由功能负责人逐功能绞杀迁移。V4 与真实生产链使用同一录制快照双跑，静态 HTML 评测台只供组长和测试验收。

**Tech Stack:** Python 3.11+、Pydantic v2、LangGraph 1.2.22、LangChain 1.5.0、pytest、TypeScript 5.8、Express 5.1、PostgreSQL、静态 HTML/JavaScript。

**Spec:** <code>docs/superpowers/specs/2026-11-26-smart-brain-v4-design.md</code>

## Global Constraints

- <code>graph/brain</code> 是错误原型：先保存可追溯现场和可用评测素材，再从主干删除；不得作为 V4 基线、回滚目标或 adapter 依赖。
- <code>brain_v3</code> 从未落地，本计划禁止创建该目录。
- app-api 是 A 股数据唯一 owner；V4 只通过内部 API 和统一工具网关取数。
- V4 禁止导入 <code>langgraph.prebuilt.create_react_agent</code>。
- 所有 run 显式携带 <code>as_of</code> 和 <code>snapshot_id</code>，节点禁止读取系统当前时间替代它们。
- 工具结果必须带 provenance；无发布时间的数据必须标低时间置信度。
- 线上方法只读；任何方法变化必须经过冻结回放集和配对 A/B。
- 所有 V4 写入使用影子表或 <code>is_shadow=true</code>，不得进入现有出口查询。
- 首阶段不修改 <code>aistock-app-frontend</code> 或 <code>aistock-frontend</code>；影子结果通过通用静态 HTML 评测台查看，不进入普通用户导航、正式查询或推送。
- 评测台必须面向所有后续 feature 使用统一 <code>EvaluationEnvelope</code>；领域专用 projection 可选，通用 JSON/section renderer 必须始终可用。
- 旧生产结果只允许进入 <code>legacy_output</code> 对照栏，不得作为 V4 evidence、ground truth 或提示词上下文。
- <code>briefing_morning</code>、<code>briefing_midday</code>、<code>briefing_evening</code> 使用独立 run、snapshot、data cutoff 和 ledger namespace，通过 parent relation 串联，禁止覆盖前一时点判断。
- <code>CanonicalEventRecord</code> 是逻辑主记录；第一阶段允许三套旧库继续存在，但只能通过 adapter 映射到统一 event_id 和时间语义。
- 议事采用 <code>SOLO_DEEP / LEAD_WITH_CONSULTANTS / ANALYST_COUNCIL / MANDATORY_RED_TEAM</code> 四种模式，模式选择由确定性 <code>CollaborationRouter</code> 决定。
- 并行分析师只能追加 submission、objection、usage 和 error；正式 HypothesisGraph、CausalGraph、CouncilDecision 和 JudgmentDraft 必须单写。
- 首轮分析师必须独立立论；第二轮开始只通过 role-scoped <code>PeerBrief</code> 读取同伴公开产物。
- 同伴观点不能作为 evidence。任何观点变化必须写 <code>BeliefUpdate</code>，并用 <code>InfluenceEdge</code> 记录影响来源。
- <code>max_research_rounds</code>、<code>max_deliberation_rounds</code> 和分析师 <code>max_steps</code> 分开配置和计费。
- 不保存原始 chain-of-thought；归档只包含结构化理由、证据引用、异议、答辩和裁决。

---

## Execution Strategy

本计划是协调主计划，必须按以下“架构先行、试点校正、接口冻结、逐功能迁移”顺序执行：

1. **公共地基**：完成 Task 0 数据库遗留确认，再实现 contracts、state、profiles、registry、Evidence Gateway、snapshot、ResearchController 和 revision。
2. **公共研究运行时**：完成 LangGraph 主图、SOLO/CONSULT/COUNCIL/RED_TEAM 路由、案卷制议会、单写提交、预算、停止和降级；使用合成案卷与冻结案例验收，不允许只有空接口。
3. **影子与评测基础设施**：完成 app-api 影子归档、<code>EvaluationEnvelope</code>、paired runner 和静态 HTML 查看器。此阶段不修改产品前端。
4. **市场溯源试点**：复用冻结事实快照，独立重建竞争假设、补证、议会、回改和 judgment；旧 <code>review.py</code> 只作为对照执行路径。
5. **简报日内链试点**：依次接入 morning、midday、evening。晚报引用 market_trace judgment，不复制盘后归因。
6. **校正与冻结**：根据两个试点修订公共契约和默认策略，发布 V4.1 接入基线；此后红区兼容优先，功能负责人不得自行修改。
7. **逐功能迁移**：每个功能使用受限 Execution Packet 和脚手架接入，完成冻结回放、盲评和工程门禁后按 1% -> 10% -> 50% -> 100% 灰度。每次只切一个 active owner。

首阶段只修改 <code>aistock-agent-py</code> 和 <code>aistock-app-api</code>。产品前端改造属于通过影子门禁后的灰度计划，不属于本计划首个试点交付。

### Execution Packet Rule

后续使用能力较弱的模型实现时，一次只交付一个受限执行包。执行包必须从 <code>docs/superpowers/plans/brain-v4-task-packet-template.md</code> 派生，填入准确的 prerequisite commit、允许文件、禁止目录、消费/产出契约、失败测试、实现步骤、验证命令和完成证据。模型不得同时读取总计划后自行选择多个 Task，也不得在允许文件之外修改代码。

公共层 Task 由组长或平台 owner 签发执行包；接口冻结后，普通功能包默认只允许修改 <code>features/&lt;feature&gt;/</code>、对应 fixtures/tests 和自己的 manifest。任何红区变更必须拆成独立平台包，先合并并发布新契约版本，功能包才能继续。

## File Structure

- <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/</code>: 研究、证据、回改、研判契约。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/graph/</code>: 状态、reducers、profiles、主图装配。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/controller/</code>: 计划、充分性、影响分析、回改、停止策略。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/tools/</code>: 工具网关、快照、provenance、memo。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/analysts/</code>: 通用分析师内核和领域 profile。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/</code>: 协作路由、议程、席位子图、异议、答辩、裁决和单写提交。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/features/</code>: 早午晚报日内链和市场溯源两个试点适配器。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/events/</code>: CanonicalEventRecord、旧事件库 adapter、去重与生命周期。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/renderers/</code>: BriefingEnvelope、App/Web/文字/音频投影。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/verification/</code>: 到期验证、错误标签、校准。
- <code>aistock-agent-py/brain_v4_evals/contracts.py</code>: 通用 <code>EvaluationEnvelope</code>、运行版本和指标契约。
- <code>aistock-agent-py/brain_v4_evals/ab/</code>: 同快照配对运行、随机盲标和晋级门禁。
- <code>aistock-agent-py/brain_v4_evals/reporting/</code>: 通用静态 HTML 生成器和领域 projection 注册。
- <code>aistock-agent-py/brain_v4_evals/reports/</code>: 生成的 <code>index.html</code>、双栏页面和 JSON 数据，不作为源码提交的默认目标。
- <code>aistock-agent-py/scripts/brain_v4/scaffold_feature.py</code>: 从 feature id、owner 和 contracts 生成绿区接入骨架。
- <code>aistock-agent-py/scripts/brain_v4/verify_change_scope.py</code>: 按 Execution Packet 校验实际修改文件和禁止目录。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/testing/contract_suite.py</code>: feature 共用的时间、幂等、降级、影子隔离和 renderer 不增添 claim 测试套件。
- <code>aistock-app-api/src/modules/brain-v4/</code>: 快照录制、影子归档和只读对比接口。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/features/&lt;feature&gt;/manifest.yaml</code>: 每个功能独占的接入声明。
- <code>aistock-agent-py/src/aistock_agent/brain_v4/generated/registry.lock</code>: CI 生成的拓扑与 owner 指纹，禁止手改。
- <code>docs/superpowers/plans/brain-v4-task-packet-template.md</code>: 较弱模型和人工开发者共同使用的受限执行包模板。

### Task 0: 冻结现场并删除错误 brain 原型

**Owner:** Orchestration Owner + Repository Maintainer

**Status (2026-11-27):** 代码、路由、配置、测试和缓存清理已完成；数据库遗留表复核因本机 PostgreSQL 不可连接而待办，Task 1 不启动。

**Files:**
- Delete: <code>aistock-agent-py/src/aistock_agent/graph/brain/</code>
- Modify: <code>aistock-agent-py/src/aistock_agent/api/routes.py</code>
- Modify: <code>aistock-agent-py/src/aistock_agent/config.py</code>
- Delete: <code>aistock-agent-py/scripts/eval_brain.py</code>
- Delete: <code>aistock-agent-py/tests/e2e/test_brain_run_api.py</code>
- Delete: <code>aistock-agent-py/tests/integration/test_brain_graph.py</code>
- Delete: <code>aistock-agent-py/tests/unit/test_brain_*.py</code>
- Delete: <code>aistock-agent-py/src/aistock_agent/services/brain_archive.py</code>, <code>services/tool_gateway.py</code>, <code>services/judgment_ledger.py</code>, <code>tools/registry_brain.py</code>, <code>schemas/judgment.py</code>
- Modify: remove prototype judgment hooks from <code>aistock-agent-py/src/aistock_agent/services/data_client.py</code> and <code>services/prediction_validator.py</code>; delete their brain-only tests
- Delete: <code>aistock-app-api/src/modules/brain/</code> and <code>/internal/brain-runs</code> routes
- Delete if never deployed; otherwise replace with a forward cleanup migration: <code>aistock-app-api/src/db/migrations/030_brain_run_archives.sql</code>

**Interfaces:**
- Produces: a repository with no runtime import, route, flag, scheduler, test or script referencing the deleted brain prototype.
- Preserves: only recorded fixtures, failure cases and reusable ideas copied into explicitly V4-named locations; no legacy runtime module survives.

- [x] **Step 1: Preserve the dirty-worktree evidence**

Record <code>git status</code>, <code>git diff --stat</code>, tracked binary patches and copies of all brain-related untracked files for both repositories under an external archive directory. Record the current commit SHA and SHA-256 manifest. Do not commit the erroneous runtime to main merely for preservation, and do not mix the archive with V4 implementation commits.

Result: archived under <code>D:/aistock/_brain_archive/2026-11-27-task0</code>; 30 manifest entries verified with zero SHA-256 mismatches.

- [ ] **Step 2: Prove the prototype has no production caller**

Search all repositories for <code>/brain/run</code>, <code>graph.brain</code>, <code>BRAIN_ENABLED</code>, <code>/internal/brain-runs</code> and <code>brain_run_archives</code>. The deletion gate requires no frontend, scheduler, app-api proxy or external service caller. If the PostgreSQL table exists, export row count and schema before dropping it through a forward cleanup migration.

Result: runtime caller scan is zero. Database probe failed with <code>ECONNREFUSED 127.0.0.1:5432</code>; keep this step open until a connected environment confirms whether <code>brain_run_archives</code> exists and, if present, exports then drops it through a forward cleanup migration.

- [x] **Step 3: Move only approved evaluation assets**

Copy useful frozen cases into <code>brain_v4_evals/cases/legacy-prototype/</code> with <code>source=deleted_brain</code> and <code>not_ground_truth=true</code>. Do not move builder, state, orchestrator, judgment schema or node code.

Result: reviewed the 24 legacy cases; they are synthetic threshold examples rather than grounded market cases. No asset was approved for V4 import; they remain recoverable only from the external archive and Git history.

- [x] **Step 4: Delete runtime code and routes**

Remove the files listed above, remove <code>brain_enabled</code>, remove route imports and unregister any documentation or AGENTS entries that present brain as an available capability. Do not delete current production workers such as morning, midday, review, broadcast, wind or event.

- [x] **Step 5: Verify deletion**

Run repository-wide searches for the deleted imports and routes; expected result is zero runtime references. Run the existing non-brain agent-py unit/integration suite and app-api typecheck/tests to prove production behavior is unchanged.

Result: runtime residue scans are zero; agent-py Ruff and mypy pass, pytest reports 5106 passed / 5 skipped; app-api TypeScript build and internal route regression pass. The app-api full suite reports 1399 passed / 3 pre-existing date-sensitive failures in <code>fear-greed.calculator.test.ts</code>, outside the Task 0 change set.

### Task 1: V4 契约与确定性 State

**Owner:** Contract Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/research.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/evidence.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/judgment.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/deliberation.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/briefing.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/events.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/graph/state.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_contracts.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_state_reducers.py</code>

**Interfaces:**
- Produces: <code>ResearchRequest</code>, <code>Hypothesis</code>, <code>CausalEdge</code>, <code>ResearchGap</code>, <code>RevisionEvent</code>, <code>RoundLedger</code>, <code>CaseFile</code>, <code>AgendaItem</code>, <code>PositionPaper</code>, <code>PeerBrief</code>, <code>BeliefUpdate</code>, <code>InfluenceEdge</code>, <code>DeliberationRoundLedger</code>, <code>Objection</code>, <code>Rebuttal</code>, <code>RevisionProposal</code>, <code>CouncilDecision</code>, <code>ProgressLedger</code>, <code>CollaborationDecision</code>, <code>V4Judgment</code>, <code>BriefingEnvelope</code>, <code>MorningBody</code>, <code>MiddayBody</code>, <code>EveningBody</code>, <code>CanonicalEventRecord</code>, <code>EventObservation</code>, <code>BrainV4State</code>.
- Reducers sort by stable business ID after merge; same ID with different content produces a conflict instead of silently keeping arrival order.

- [ ] **Step 1: Write failing contract tests**

~~~python
from pydantic import ValidationError
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest


def test_request_requires_as_of_and_snapshot():
    try:
        ResearchRequest(profile="market_trace", subject_type="market", subject_code="000001-SH")
    except ValidationError as exc:
        assert "as_of" in str(exc)
        assert "snapshot_id" in str(exc)
    else:
        raise AssertionError("missing time boundary must fail")


def test_hypothesis_revision_is_versioned():
    hypothesis = Hypothesis(
        hypothesis_id="hyp_liquidity",
        version=2,
        statement="流动性是放大器",
        status="weakened",
        parent_ids=["hyp_liquidity:v1"],
        disconfirmers=["成长与防御同步下跌"],
    )
    assert hypothesis.ref == "hyp_liquidity:v2"


def test_midday_briefing_requires_parent_judgment():
    with pytest.raises(ValidationError):
        BriefingEnvelope(profile="briefing_midday", parent_judgment_ids=[])


def test_event_record_separates_event_from_observations():
    event = canonical_event_with_news_announcement_and_calendar()
    assert event.event_id
    assert len(event.observation_ids) == 3
    assert not hasattr(event, "beneficiary_sector")
~~~

- [ ] **Step 2: Run tests and verify red state**

Run: <code>uv run pytest tests/unit/brain_v4/test_contracts.py -q</code>  
Expected: collection fails because <code>aistock_agent.brain_v4</code> does not exist.

- [ ] **Step 3: Implement the models with extra=forbid**

~~~python
class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: str
    version: int = Field(ge=1)
    statement: str
    status: Literal["open", "supported", "weakened", "rejected", "frozen"]
    parent_ids: list[str] = Field(default_factory=list)
    disconfirmers: list[str] = Field(min_length=1)

    @property
    def ref(self) -> str:
        return f"{self.hypothesis_id}:v{self.version}"
~~~

- [ ] **Step 4: Add reducer tests for arrival-order independence**

Run: <code>uv run pytest tests/unit/brain_v4/test_state_reducers.py -q</code>  
Expected: two merge orders produce byte-equivalent model dumps.

- [ ] **Step 5: Run contract and reducer tests**

Run: <code>uv run pytest tests/unit/brain_v4/test_contracts.py tests/unit/brain_v4/test_state_reducers.py -q</code>  
Expected: PASS.

### Task 2: Profiles、运行隔离与停止策略

**Owner:** Orchestration Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/graph/profiles.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/controller/stop_policy.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/contracts/manifest.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/registry.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/briefing/manifest.yaml</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/market_trace/manifest.yaml</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/generated/registry.lock</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_profiles.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_stop_policy.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_registry.py</code>

**Interfaces:**
- Produces: <code>RunProfile</code> and <code>StopDecision evaluate_stop(RoundLedger, BudgetState)</code>.
- Produces: <code>FeatureManifest</code>, <code>load_registry(root: Path) -&gt; Registry</code>, <code>validate_registry(registry) -&gt; list[RegistryError]</code> and <code>generate_registry_lock(registry) -&gt; str</code>.
- Profiles: <code>alert_hot</code>, <code>briefing_morning</code>, <code>briefing_midday</code>, <code>briefing_evening</code>, <code>market_trace</code>, <code>intraday</code>, <code>on_demand_deep</code>, <code>verify</code>.

- [ ] **Step 1: Write profile isolation tests**

~~~python
def test_alert_hot_cannot_start_deep_loop():
    profile = get_profile("alert_hot")
    assert profile.max_research_rounds == 1
    assert profile.max_deliberation_rounds == 0
    assert profile.allow_case_memory is False


def test_review_and_intraday_use_different_ledger_namespaces():
    assert ledger_namespace("briefing_evening", "run-1") != ledger_namespace("intraday", "run-1")


def test_briefing_profiles_have_distinct_cutoffs_and_contracts():
    assert get_profile("briefing_morning").output_contract == "MorningBody"
    assert get_profile("briefing_midday").requires_parent_judgment is True
    assert get_profile("briefing_evening").required_subprofiles == ["market_trace"]
~~~

- [ ] **Step 2: Run and verify failure**

Run: <code>uv run pytest tests/unit/brain_v4/test_profiles.py -q</code>  
Expected: FAIL because profile registry is missing.

- [ ] **Step 3: Implement immutable profile registry**

Each profile declares exact <code>max_research_rounds</code>, <code>max_deliberation_rounds</code>, analyst <code>max_steps</code>, <code>max_revisions</code>, <code>timeout_s</code>, <code>token_budget</code>, tool groups, data-cutoff policy, parent requirements and output contract. No node reads defaults outside the selected profile.

- [ ] **Step 4: Implement stop policy**

~~~python
def evaluate_stop(ledger: RoundLedger, budget: BudgetState) -> StopDecision:
    if ledger.sufficiency_passed:
        return StopDecision(stop=True, reason="sufficient")
    if ledger.stall_count >= 2:
        return StopDecision(stop=True, reason="stall")
    if ledger.oscillation_detected:
        return StopDecision(stop=True, reason="unstable")
    if budget.exhausted:
        return StopDecision(stop=True, reason="budget")
    return StopDecision(stop=False, reason="continue")
~~~

- [ ] **Step 5: Verify profile and stop-policy tests**

Run: <code>uv run pytest tests/unit/brain_v4/test_profiles.py tests/unit/brain_v4/test_stop_policy.py -q</code>  
Expected: PASS.

- [ ] **Step 6: Write distributed-registry tests**

Add tests proving: duplicate feature IDs fail; dependency cycles fail; an output cannot have two active owners; a feature directory without manifest fails; a manifest without directory fails; two generations from the same sorted manifests produce an identical SHA-256 lock.

- [ ] **Step 7: Implement manifests and generated registry**

Define <code>FeatureManifest</code> with id, kind, owner, status, profiles, contracts, capabilities, feature dependencies, cutoff, budgets, degradation, active owner, shadow owner and legacy deletion metadata. Discover manifests under each feature/capability directory, validate them, topologically sort dependencies and generate <code>registry.lock</code>. Runtime composition reads the validated registry; feature modules do not edit a central builder or central YAML file.

- [ ] **Step 8: Verify registry generation**

Run: <code>uv run pytest tests/unit/brain_v4/test_registry.py -q</code>  
Run <code>uv run python -m aistock_agent.brain_v4.registry --write-lock</code> twice and compare SHA-256.  
Expected: tests pass, hashes match, and the committed lock has no diff after regeneration.

### Task 3: 统一证据网关、事件主记录与可复现快照

**Owner:** Evidence Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/tools/gateway.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/tools/provenance.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/tools/memo.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/events/canonical.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/events/adapters.py</code>
- Modify: <code>aistock-app-api/src/core/routes/internal.ts</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_tool_gateway.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_canonical_events.py</code>
- Test: <code>aistock-app-api/src/modules/brain-v4/__tests__/snapshot.spec.ts</code>

**Interfaces:**
- Consumes: V4 <code>ResearchRequest</code>.
- Produces: <code>ToolEnvelope</code> containing <code>event_time</code>, <code>published_at</code>, <code>fetched_at</code>, <code>as_of</code>, quality and classified error.
- Produces one <code>CanonicalEventRecord</code> per real-world event and one or more <code>EventObservation</code> rows for news, announcements, research and calendar sources. The canonical record cannot contain impact conclusions.

- [ ] **Step 1: Write a future-data rejection test**

~~~python
async def test_gateway_rejects_published_after_as_of(fake_client):
    gateway = ToolGateway(fake_client, as_of="2026-11-26T10:00:00+08:00")
    result = await gateway.call("news", {"symbol": "600519-SH"})
    assert result.error == "future_data"
    assert result.data is None
~~~

- [ ] **Step 2: Write memo deduplication test**

Two concurrent calls with identical normalized arguments must make one internal HTTP request and return the same evidence ID.

- [ ] **Step 3: Implement gateway middleware**

The gateway normalizes symbols, sorts request keys, computes the memo key, enforces tool allowlists, applies timeout, validates time boundaries and emits trace counters.

- [ ] **Step 4: Implement canonical event adapters**

Map agent <code>event_store</code>, app-api <code>event_entities</code> and calendar rows into the same event contract. Tests must prove that three records describing one event resolve to one <code>event_id</code> with three observations; a correction creates a lifecycle version; an event published after price initiation cannot be marked as the initial trigger.

- [ ] **Step 5: Add app-api snapshot recording endpoint**

Create an internal endpoint that accepts a list of tool requests and returns a frozen response manifest with SHA-256. The response must be replayable without live upstream calls.

- [ ] **Step 6: Verify Python and TypeScript tests**

Run: <code>uv run pytest tests/unit/brain_v4/test_tool_gateway.py tests/unit/brain_v4/test_canonical_events.py -q</code>  
Run: <code>pnpm test -- src/modules/brain-v4/__tests__/snapshot.spec.ts</code> in <code>aistock-app-api</code>.  
Expected: PASS.

### Task 4: ResearchController、Gap、影响分析与局部回改

**Owner:** Judgment Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/controller/planner.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/controller/sufficiency.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/controller/revision.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/controller/impact.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_revision.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_sufficiency.py</code>

**Interfaces:**
- Produces: <code>ResearchPlan</code>, <code>SufficiencyReport</code>, <code>RevisionDecision</code>, impacted hypothesis and claim IDs.
- Revision actions are exactly: add evidence, reweight, revise, split, merge, reject.

- [ ] **Step 1: Write the market amplifier regression test**

~~~python
def test_liquidity_root_is_revised_to_amplifier_when_style_diverges():
    decision = revise(
        hypothesis=liquidity_root_v1(),
        contradictions=[defensive_up_growth_down()],
        graph=market_graph(),
    )
    assert decision.action == "revise_hypothesis"
    assert decision.new_hypothesis.statement == "流动性是放大器"
    assert decision.impacted_claim_ids == ["claim_market_root", "claim_repricing"]
~~~

- [ ] **Step 2: Write oscillation and revision-budget tests**

A→B→A must stop with <code>unstable</code>. A third revision of the same hypothesis must be rejected by policy.

- [ ] **Step 3: Implement deterministic impact traversal**

Traverse causal edges from the revised hypothesis and return descendants only. Do not invalidate unrelated evidence or sibling hypotheses.

- [ ] **Step 4: Implement structured sufficiency gate**

The gate checks evidence coverage, time order, mechanism, magnitude, alternative explanations, invalidators and unsupported-edge ratio. It returns gaps; it never writes narrative text.

- [ ] **Step 5: Verify controller tests**

Run: <code>uv run pytest tests/unit/brain_v4/test_revision.py tests/unit/brain_v4/test_sufficiency.py -q</code>  
Expected: PASS.

### Task 5: 案卷制分析师议会与协作路由

**Owner:** Deliberation/Judgment Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/router.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/agenda.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/roles.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/peer_briefs.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/beliefs.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/convergence.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/graph.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/judge.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/deliberation/committer.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_collaboration_router.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_deliberation_reducers.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_belief_updates.py</code>
- Test: <code>aistock-agent-py/tests/integration/brain_v4/test_analyst_council.py</code>

**Interfaces:**
- Consumes: <code>CaseFile</code>, <code>SufficiencyReport</code>, hypothesis ranking, conflicts and selected <code>RunProfile</code>.
- Produces: <code>CollaborationDecision</code>, <code>PositionPaper[]</code>, <code>PeerBrief[]</code>, <code>BeliefUpdate[]</code>, <code>InfluenceEdge[]</code>, <code>DeliberationRoundLedger[]</code>, <code>Objection[]</code>, <code>Rebuttal[]</code>, <code>RevisionProposal[]</code>, <code>CouncilDecision</code> and <code>ProgressLedger[]</code>.
- Only <code>RevisionCommitter.commit(proposal, state)</code> may change canonical hypothesis and causal graphs. <code>CouncilJudge</code> cannot add evidence.

- [ ] **Step 1: Write collaboration-router tests**

~~~python
def test_close_material_hypotheses_trigger_council():
    decision = route_collaboration(
        materiality="high",
        hypothesis_scores=[0.56, 0.51, 0.18],
        evidence_conflict_score=0.72,
        cross_layer_conflict=True,
        remaining_budget=40_000,
    )
    assert decision.mode == "ANALYST_COUNCIL"
    assert "close_competing_hypotheses" in decision.trigger_codes


def test_alert_hot_never_starts_council():
    decision = route_collaboration(profile="alert_hot", materiality="high")
    assert decision.mode == "SOLO_DEEP"
    assert decision.selected_roles == []
~~~

- [ ] **Step 2: Run router tests and verify failure**

Run: <code>uv run pytest tests/unit/brain_v4/test_collaboration_router.py -q</code>  
Expected: FAIL because <code>route_collaboration</code> does not exist.

- [ ] **Step 3: Implement deterministic routing and role selection**

Use profile thresholds for hypothesis-score margin, evidence conflict, materiality, cross-layer conflict and remaining budget. Model output may populate <code>suggested_mode</code> but cannot overwrite <code>mode</code>.

- [ ] **Step 4: Write reducer and single-writer tests**

~~~python
def test_parallel_positions_are_order_independent():
    left = merge_submissions([position_b], [position_a])
    right = merge_submissions([position_a], [position_b])
    assert left == right


def test_analyst_cannot_write_canonical_graph():
    update = run_role_node("alternative", frozen_case())
    assert "hypothesis_graph" not in update
    assert "causal_graph" not in update
    assert list(update) == ["submissions", "usage"]
~~~

- [ ] **Step 5: Write peer-influence and anti-herding tests**

~~~python
def test_round_zero_positions_cannot_see_peers():
    context = build_role_context(round_no=0, role="mechanism", state=council_state())
    assert context.peer_briefs == []


def test_peer_opinion_without_evidence_cannot_raise_confidence():
    update = validate_belief_update(
        prior_confidence=0.55,
        posterior_confidence=0.70,
        influencing_artifact_ids=["pos_alt_1"],
        evidence_ids=[],
    )
    assert update.accepted is False
    assert update.reason == "confidence_increase_requires_evidence_or_mechanism_change"


def test_peer_brief_can_redirect_research():
    update = belief_update_from_peer_brief(peer_brief_with_timeline_conflict())
    assert update.action == "weaken"
    assert update.requested_gap_ids == ["gap_pre_move_trigger"]
    assert update.influence_edges[0].influence_type == "search_direction"
~~~

- [ ] **Step 6: Implement PeerBrief, BeliefUpdate and InfluenceEdge**

Generate role-scoped briefs after R0 only. Every active seat must emit a belief update, including <code>hold</code>. Store prior and posterior hypothesis references and confidence; never overwrite earlier rounds. An influence edge records the source artifact, target update, effect and whether it is evidence-backed.

- [ ] **Step 7: Implement the multi-round deliberation StateGraph**

Graph path: <code>case_freeze → agenda_build → r0_independent_positions → fact_normalize → peer_brief_reveal → cross_exam → belief_update → convergence_gate</code>. The gate routes to targeted research, another deliberation round, judge, insufficient or escalation. New evidence increments <code>case_version</code> and reruns only affected seats. Use <code>Send</code> for seats, append-only reducers for public artifacts, private subgraph state for messages/scratch and <code>Command</code> for routing.

- [ ] **Step 8: Enforce objection, convergence and judge invariants**

Reject objections without <code>target_id</code>, <code>severity</code> or <code>kill_criterion</code>. Reject a <code>CouncilDecision</code> that references evidence absent from the frozen <code>EvidenceLedger</code>. Continue only when there is new evidence, an unresolved high-severity objection, changed topology or a material belief delta; otherwise mark the round <code>no_progress</code> and stop according to profile policy.

- [ ] **Step 9: Test a complete multi-round council case**

Use a market fixture where liquidity and risk-preference hypotheses are close. Assert R0 positions are created before peer visibility; R1 peer briefs cause one analyst to weaken its hypothesis and request a timeline gap; targeted research increments <code>case_version</code>; only affected seats rerun; R2 ranking stabilizes; one revision proposal is committed by the single writer; the judge preserves a minority opinion; and the final state contains no concurrent-update conflict.

- [ ] **Step 10: Verify deliberation tests**

Run: <code>uv run pytest tests/unit/brain_v4/test_collaboration_router.py tests/unit/brain_v4/test_deliberation_reducers.py tests/unit/brain_v4/test_belief_updates.py tests/integration/brain_v4/test_analyst_council.py -q</code>  
Expected: PASS.

### Task 6: LangGraph 主图与分析师内核

**Owner:** Orchestration Owner + Domain Owners

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/graph/builder.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/analysts/kernel.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/analysts/profiles.py</code>
- Test: <code>aistock-agent-py/tests/integration/brain_v4/test_research_loop.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_no_deprecated_agent_api.py</code>

**Interfaces:**
- Graph path: START → profile → plan → dispatch → collect → analyze → assess → collaboration_router; router selects solo, consult, council or red-team, then routes to revise, decision gate or END.
- Analyst output is structured <code>AnalystResult</code>; it cannot write final judgment.

- [ ] **Step 1: Write topology and deprecated-import tests**

~~~python
def test_v4_never_imports_create_react_agent():
    source = read_all_python("src/aistock_agent/brain_v4")
    assert "create_react_agent" not in source
~~~

- [ ] **Step 2: Build the graph with conditional edges**

Use <code>Send</code> for hypothesis/tool/analyst-seat fan-out and <code>Command</code> for revision and deliberation routing. Every parallel channel has an explicit reducer; canonical graphs have no reducer because they are single-writer fields.

- [ ] **Step 3: Create the bounded analyst**

Use <code>langchain.agents.create_agent</code> with per-profile tools, structured response format, model-call and tool-call middleware, and an exact step ceiling from <code>RunProfile</code>.

- [ ] **Step 4: Test a two-round run**

Research round 1 must create a gap; research round 2 must resolve it and select a collaboration mode. When council is selected, deliberation R0 creates independent positions and R1 records peer briefs plus belief updates before judgment commit. Assert research and deliberation counters are separate, trace contains both ledger types and no duplicate tool call.

- [ ] **Step 5: Verify graph tests**

Run: <code>uv run pytest tests/integration/brain_v4/test_research_loop.py tests/unit/brain_v4/test_no_deprecated_agent_api.py -q</code>  
Expected: PASS.

### Task 7: 影子归档、静态评测台与受限执行工具

**Owner:** Output Owner + Learning/Eval Owner + Repository Maintainer

**Files:**
- Create: <code>aistock-app-api/src/db/migrations/031_brain_v4_shadow.sql</code>
- Create: <code>aistock-app-api/src/modules/brain-v4/BrainV4ArchiveService.ts</code>
- Create: <code>aistock-app-api/src/modules/brain-v4/internalRouter.ts</code>
- Modify: <code>aistock-app-api/src/index.ts</code>
- Create: <code>aistock-app-api/src/modules/brain-v4/__tests__/internalRouter.spec.ts</code>
- Create: <code>aistock-agent-py/brain_v4_evals/contracts.py</code>
- Create: <code>aistock-agent-py/brain_v4_evals/ab/run_paired.py</code>
- Create: <code>aistock-agent-py/brain_v4_evals/reporting/generate.py</code>
- Create: <code>aistock-agent-py/brain_v4_evals/reporting/static/index.js</code>
- Create: <code>aistock-agent-py/brain_v4_evals/reporting/static/styles.css</code>
- Create: <code>aistock-agent-py/scripts/brain_v4/scaffold_feature.py</code>
- Create: <code>aistock-agent-py/scripts/brain_v4/verify_change_scope.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/testing/contract_suite.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_evaluation_envelope.py</code>
- Test: <code>aistock-agent-py/tests/integration/brain_v4/test_shadow_isolation.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_execution_packet_tools.py</code>

**Interfaces:**
- <code>EvaluationEnvelope</code> contains feature/profile/subject, legacy and V4 run IDs, exact snapshot SHA, versions, outputs, evidence summary, hypotheses, revisions, metrics and reviewer state.
- app-api persists exact <code>as_of TIMESTAMPTZ</code>, profile, snapshot, parent relation, config/method version and <code>is_shadow</code>; shadow rows are excluded from every production query.
- <code>generate_report(envelopes, output_dir)</code> creates a browser-openable <code>index.html</code>, per-feature comparison pages and JSON data without a frontend service.
- <code>verify_change_scope(packet, changed_paths)</code> fails when a model edits a file outside <code>allowed_files</code> or inside <code>forbidden_paths</code>.

- [ ] **Step 1: Write failing shadow and envelope tests**

Assert a shadow judgment never appears in active queries; mismatched snapshot SHA cannot form a comparison; unknown envelope fields fail validation; the legacy output cannot be copied into V4 evidence IDs.

- [ ] **Step 2: Add the isolated app-api module**

Create physical shadow tables or a database-enforced shadow predicate, indexes on profile/subject/as_of/run ID, archive and read-only comparison services, and mount <code>/internal/brain-v4</code> from <code>src/index.ts</code>. Do not add routes to the large <code>core/routes/internal.ts</code> file and do not reuse deleted <code>brain_run_archives</code>.

- [ ] **Step 3: Implement paired envelope creation and static HTML generation**

The paired runner accepts only matching snapshot hashes, randomizes blind labels and writes immutable JSON. The static viewer provides date, feature and version filters; old/V4 dual columns; snapshot/version metadata; evidence, hypothesis, revision and trace sections; latency/token/cost metrics; and blind-review fields. It contains no research logic and makes no live data calls.

- [ ] **Step 4: Implement execution safeguards**

Add a feature scaffold that generates only <code>manifest.yaml</code>, <code>profile.py</code>, <code>adapter.py</code>, <code>projection.py</code>, <code>fixtures/</code> and <code>tests/</code>. Add scope verification against a filled Execution Packet and a reusable contract suite covering as_of, snapshot, idempotency, partial/insufficient, shadow isolation and renderer claim preservation.

- [ ] **Step 5: Verify Task 7**

Run: <code>node --import tsx --test src/modules/brain-v4/__tests__/internalRouter.spec.ts</code> in <code>aistock-app-api</code>.  
Run: <code>pnpm build</code> in <code>aistock-app-api</code>.  
Run: <code>uv run pytest tests/unit/brain_v4/test_evaluation_envelope.py tests/integration/brain_v4/test_shadow_isolation.py tests/unit/brain_v4/test_execution_packet_tools.py -q</code>.  
Expected: PASS; generated report opens without a server and contains both comparison columns.

### Task 8: 市场溯源试点与公共架构第一次校正

**Owner:** Market Owner + Orchestration Owner + Deliberation/Judgment Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/market_trace/manifest.yaml</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/market_trace/profile.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/market_trace/adapter.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/market_trace/projection.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/market_trace/evaluation.py</code>
- Test: <code>aistock-agent-py/tests/integration/brain_v4/test_market_trace.py</code>
- Test: <code>aistock-agent-py/tests/integration/brain_v4/test_market_trace_comparison.py</code>

**Interfaces:**
- Consumes the existing frozen <code>MarketTraceSnapshot</code> only through an adapter; it does not import legacy review state or final conclusions.
- Produces competing hypotheses, causal graph, rejected alternatives, gaps, revisions, minority opinions, depth level, six-stage projection and <code>EvaluationEnvelope</code>.

- [ ] **Step 1: Write the anti-contamination test**

Fail if a V4 evidence ID points to legacy review narrative or if the adapter imports <code>agents.workers.review</code>. Permit reuse of deterministic snapshot data and provenance only.

- [ ] **Step 2: Implement and test independent research**

Use a frozen case where liquidity initially appears causal but sector/style divergence shows it is an amplifier. Require at least three competing hypotheses, a structured Gap, targeted new evidence, one local revision and no unsupported high-confidence edge.

- [ ] **Step 3: Exercise the complete council path**

Use a close-score case that routes to <code>ANALYST_COUNCIL</code>. Assert R0 independence, role-scoped PeerBrief, evidence-backed BeliefUpdate, preserved minority opinion, single-writer commit and bounded termination.

- [ ] **Step 4: Generate the first real dual-column report**

Run legacy review and V4 against the same snapshot SHA, archive both outputs, generate static HTML and verify that the report exposes conclusion, evidence, alternatives, revision, insufficient state, latency and cost without changing any production query.

- [ ] **Step 5: Record public-interface corrections**

Every required public change must be written as a separate contract decision with the failing market case ID. Do not silently change shared types inside the feature PR.

- [ ] **Step 6: Verify Task 8**

Run: <code>uv run pytest tests/integration/brain_v4/test_market_trace.py tests/integration/brain_v4/test_market_trace_comparison.py -q</code>.  
Expected: PASS and a generated market-trace HTML comparison for every frozen case.

### Task 9: 早午晚报日内链试点与接口冻结

**Owner:** Briefing Owner + Market Owner + Output Owner + Contract Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/briefing/manifest.yaml</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/briefing/profiles.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/briefing/adapter.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/briefing/projection.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/features/briefing/evaluation.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/renderers/briefing.py</code>
- Test: <code>aistock-agent-py/tests/integration/brain_v4/test_briefing_chain.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_briefing_renderer.py</code>
- Modify: <code>docs/智慧大脑-V4-团队接入与迁移规范.md</code>

**Interfaces:**
- Morning produces baseline, calendar, sector/style hypotheses, scenarios and invalidators within its cutoff.
- Midday reads morning judgment plus AM evidence and emits explicit confirm/weaken/revise/reverse relations without rewriting unchanged claims.
- Evening references market-trace judgment by ID, scores morning/midday claims and projects one result to App/Web/text/audio contracts.

- [ ] **Step 1: Implement and test morning cutoff isolation**

Use a fixture with an important item published after the morning cutoff. Assert it is absent from morning evidence; no same-day observation is visible before event time; at least one falsifiable scenario and invalidator remains.

- [ ] **Step 2: Implement and test midday incremental revision**

Add an AM snapshot that confirms one morning hypothesis and rejects another. Assert unchanged evidence is reused, only impacted hypotheses reopen, and every change contains <code>parent_judgment_id</code> plus relation.

- [ ] **Step 3: Implement and test evening reuse**

Assert evening references the Task 8 market-trace result, scores each material morning/midday claim and cannot create a second independent primary-cause judgment.

- [ ] **Step 4: Test cross-channel contract preservation**

Render one evening judgment into App, Web, text and audio projections. Headline, direction, relation, risk level and evidence IDs must match; audio may shorten wording but cannot add a claim or change confidence. No product frontend files are modified in this task.

- [ ] **Step 5: Generate briefing comparisons and freeze V4.1**

Generate morning, midday and evening dual-column reports for frozen days. Resolve public contract gaps exposed by both pilots, bump exact contract versions, regenerate registry/schema outputs, update the team migration baseline and mark red-zone interfaces compatibility-first.

- [ ] **Step 6: Verify Task 9**

Run: <code>uv run pytest tests/integration/brain_v4/test_briefing_chain.py tests/unit/brain_v4/test_briefing_renderer.py -q</code>.  
Expected: PASS; three reports per day share the correct parent chain and evening references market trace.

### Task 10: 验证学习、配对 A/B、灰度门禁与试点报告

**Owner:** Learning/Eval Owner + Orchestration Owner

**Files:**
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/verification/verifier.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/verification/error_tags.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/learning/candidates.py</code>
- Create: <code>aistock-agent-py/src/aistock_agent/brain_v4/learning/deliberation_metrics.py</code>
- Create: <code>aistock-agent-py/brain_v4_evals/ab/gates.py</code>
- Create: <code>aistock-agent-py/brain_v4_evals/cases/index.json</code>
- Create: <code>aistock-agent-py/brain_v4_evals/reports/pilot-report.md</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_verification.py</code>
- Test: <code>aistock-agent-py/tests/unit/brain_v4/test_ab_gates.py</code>

- [ ] **Step 1: Freeze initial cases**

Briefing requires at least 10 complete trading days. Market trace requires at least 10 cases, including at least 3 known shallow/failure cases and one council plus local-revision case. Record case SHA, snapshot SHA and expected time visibility, not expected V4 conclusions.

- [ ] **Step 2: Implement append-only verification and error tags**

The verifier never mutates original judgments. A correct direction with wrong primary cause receives <code>cause_wrong</code> but not <code>direction_wrong</code>; high-confidence misses also receive <code>overconfidence</code>. Method candidates remain draft and cannot update production methods.

- [ ] **Step 3: Implement promotion gates**

~~~python
def test_small_sample_cannot_promote():
    result = evaluate_promotion(sample_size=19, quality_delta=0.2, p95_delta=-0.3)
    assert result.promote is False
    assert result.reason == "sample_too_small"
~~~

Require zero time leakage, matching snapshots, no production contamination, minimum sample size, quality threshold, acceptable P95/cost delta and a tested rollback before promotion.

- [ ] **Step 4: Run static, V4 and production regression suites**

Run: <code>uv run ruff check src/aistock_agent/brain_v4 brain_v4_evals tests/unit/brain_v4 tests/integration/brain_v4</code>.  
Run: <code>uv run mypy src/aistock_agent/brain_v4 brain_v4_evals</code>.  
Run: <code>uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q</code>.  
Run app-api <code>pnpm build</code> and the complete app-api test command. Then run current morning, midday, review, evening/broadcast, wind and event regressions. Expected: zero new failures; pre-existing failures remain explicitly baselined.

- [ ] **Step 5: Generate the pilot and handoff report**

Report sample counts, blind scores, depth, unsupported edges, research/deliberation rounds, belief changes, revisions, P50/P95, token/cost, insufficient rate, duplicate tools, collaboration-mode value, time leakage, midday effective change, parent accuracy, evening scorecard coverage and projection consistency. Include failed cases, frozen V4.1 contract versions, sample Execution Packets, rollback instructions and the first list of features approved for team migration. Do not recommend promotion unless all quality and engineering gates pass.

