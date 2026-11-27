"""V4 研究主图：研究循环 + 协作路由 + 议会 + 单写研判提交。

主路径（计划 Task 6）：
`START -> profile -> plan -> dispatch -> collect -> analyze -> assess
 -> collaboration_router`，随后按确定性路由进入 `solo / consult / council /
 red_team`，再经 `revise / decision_gate / END`。

通道纪律（团队接入规范 §2.8）：
- 并行扇出只用 `Send`（假设 × 工具组、分析师席位）；
- 并行通道 `evidence_items / analyst_results / usage / errors / trace` 必须挂
  显式 reducer，并复用 `graph/state.py` 的 `merge_by_id / append_events /
  sum_usage`；
- `hypothesis_graph / causal_graph / council_decision / judgment_draft` 等单写
  通道不挂 reducer；
- 回改与议会推进用 `Command` 路由。

时间纪律：所有节点只从 state 透传 `as_of / data_cutoff / snapshot_id`；
研判的 `created_at` 取 `request.as_of`，绝不读取系统当前时间。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict, TypeVar, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send

from ..analysts.context import build_analyst_task
from ..analysts.kernel import AnalystResult, run_analyst_async
from ..analysts.profiles import analyst_seats, analyst_tool_groups
from ..analysts.tools import build_analyst_tools
from ..contracts import V4Contract
from ..contracts.deliberation import (
    BeliefUpdate,
    CaseFile,
    CollaborationDecision,
    CouncilDecision,
    DeliberationRoundLedger,
    InfluenceEdge,
    Objection,
    PeerBrief,
    PositionPaper,
    Rebuttal,
    RevisionProposal,
)
from ..contracts.evidence import EvidenceItem, EvidenceLedger
from ..contracts.judgment import V4Judgment
from ..contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchGap,
    ResearchRequest,
    RevisionEvent,
    RoundLedger,
)
from ..controller.planner import ResearchPlan, build_plan
from ..controller.sufficiency import evaluate_sufficiency
from ..deliberation.graph import DeliberationState, build_council_graph
from ..deliberation.router import route_collaboration
from ..graph.profiles import RunProfile, get_profile
from ..graph.state import append_events, canonical_digest, merge_by_id, sum_usage
from ..tools.gateway import ToolClient, ToolGateway
from ..tools.tool_map import TOOL_GROUPS, TOOL_MAP, expand_groups

#: 分析师步数上限：不持有取数工具后，3 步足够「读证据 → 综合 → 结构化输出」，
#: 不再随 profile 配置的工具数量放大。
_ANALYST_MAX_STEPS = 3

#: 单个席位的最低可用预算：实测一次主调用就要 5.7k~7.2k token，低于该值必然
#: 首调用即 budget_exhausted（2026-11-27 晚报 5 席位各 4800 → 全部 stalled）。
_MIN_SEAT_BUDGET = 8000


def _plan_seats(
    seats: Sequence[str], remaining: int
) -> tuple[list[str], int]:
    """按剩余预算裁剪席位数并均分预算；返回 (席位, 每席位预算)。

    为什么不能直接 ``remaining // len(seats)``：席位越多每席位越少，一旦
    低于「一次主调用」的最低成本，所有席位都会在第一步耗尽预算，花了 token
    却拿不到任何结构化结论。席位不足时优先保留靠前（主分析师优先）的席位。
    """
    if not seats:
        return [], remaining
    if remaining <= 0:
        return [seats[0]], 0
    affordable = max(1, remaining // _MIN_SEAT_BUDGET)
    chosen = list(seats[: min(len(seats), affordable)])
    return chosen, remaining // len(chosen)

ModelT = TypeVar("ModelT", bound=V4Contract)
StateT = TypeVar("StateT")

#: 议会/红队模式在提交研判前必须具备的先行产物。
_DELIBERATION_MODES: frozenset[str] = frozenset(
    {"ANALYST_COUNCIL", "MANDATORY_RED_TEAM"}
)


def _merge_models(
    left: Sequence[ModelT],
    right: Sequence[ModelT],
    *,
    model: type[ModelT],
    id_field: str,
) -> list[ModelT]:
    merged = merge_by_id(
        [item.model_dump() for item in left],
        [item.model_dump() for item in right],
        id_field,
    )
    return [model.model_validate(item) for item in merged.items]


def _merge_evidence(
    left: Sequence[EvidenceItem], right: Sequence[EvidenceItem]
) -> list[EvidenceItem]:
    return _merge_models(left, right, model=EvidenceItem, id_field="evidence_id")


def _merge_analyst_results(
    left: Sequence[AnalystResult], right: Sequence[AnalystResult]
) -> list[AnalystResult]:
    return _merge_models(
        left, right, model=AnalystResult, id_field="result_id"
    )


def _merge_usage(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return sum_usage(left, right)


def _merge_errors(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return append_events(left, right, "error_id")


def _merge_trace(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return append_events(left, right, "trace_id")


def _analyst_tokens_used(state: Mapping[str, Any]) -> float:
    """累计本次 run 已消耗的分析师 token（usage 通道按 key 求和）。"""
    total = 0.0
    for record in state.get("usage", ()):
        if not isinstance(record, Mapping) or record.get("key") != "analyst_tokens":
            continue
        value = record.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            total += float(value)
    return total


def _budget_allows_new_round(
    state: Mapping[str, Any], profile: RunProfile
) -> bool:
    """run 级累计预算是否还够再派发一整轮分析师。

    为什么不能只看 ``remaining > 0``：分析师席位是并行扇出的，只要派发就会
    产生一次模型调用；仅当剩余预算不小于「已完成轮次的平均成本」时，再开一轮
    才不至于把累计 token 顶过 ``profile.token_budget`` 的 1.2 倍上限。
    """
    used = _analyst_tokens_used(state)
    remaining = profile.token_budget - used
    if remaining <= 0:
        return False
    rounds = int(state.get("research_round_no", 0))
    if rounds <= 0:
        return True
    return remaining >= used / rounds


class ResearchTask(TypedDict, total=False):
    """单个“假设 × 工具组”研究任务，通过 `Send` 并行下发。"""

    run_id: str
    subject_code: str
    round_no: int
    tool_group: str
    hypothesis_ref: str
    as_of: datetime
    data_cutoff: datetime
    snapshot_id: str


class AnalystTask(TypedDict, total=False):
    """单个分析师席位子任务，通过 `Send` 并行下发。"""

    role: str
    round_no: int
    profile_name: str
    subject_code: str
    hypothesis_refs: list[str]
    evidence_ids: list[str]
    task_text: str
    #: run 级剩余预算（由 `dispatch_analysis` 按累计 usage 计算后下发）。
    token_budget: int


class ResearchGraphState(TypedDict, total=False):
    """V4 主图状态；并行通道挂显式 reducer，单写通道不挂。"""

    request: ResearchRequest
    case_file: CaseFile
    evidence_ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    #: 证据正文映射（evidence_id / tool_params.source_id → 文本）。必须是显式
    #: state 通道：LangGraph 会丢弃未声明的键，之前漏声明导致分析师只看到
    #: tool_params 元数据，claim 误报“缺少具体数值”（2026-11-27 实测）。
    evidence_sources: dict[str, str]
    hypothesis_graph: dict[str, Hypothesis]
    causal_graph: list[CausalEdge]
    plan: ResearchPlan
    plan_id: str
    research_gaps: list[ResearchGap]
    round_ledgers: list[RoundLedger]
    research_round_no: int
    deliberation_round_no: int
    case_version: int
    sufficiency_passed: bool
    publish_ready: bool
    counter_evidence_coverage: float
    parent_judgment_ids: list[str]
    evidence_items: Annotated[list[EvidenceItem], _merge_evidence]
    analyst_results: Annotated[list[AnalystResult], _merge_analyst_results]
    submissions: list[PositionPaper]
    objections: list[Objection]
    rebuttals: list[Rebuttal]
    peer_briefs: list[PeerBrief]
    belief_updates: list[BeliefUpdate]
    influence_edges: list[InfluenceEdge]
    revision_proposals: list[RevisionProposal]
    revision_events: list[RevisionEvent]
    deliberation_round_ledgers: list[DeliberationRoundLedger]
    council_decision: CouncilDecision
    collaboration_decision: CollaborationDecision
    judgment_draft: V4Judgment
    run_status: str
    stop_reason: str
    usage: Annotated[list[dict[str, Any]], _merge_usage]
    errors: Annotated[list[dict[str, Any]], _merge_errors]
    trace: Annotated[list[dict[str, Any]], _merge_trace]


class JudgmentEvidenceError(ValueError):
    """研判引用了冻结 `EvidenceLedger` 之外的证据。"""


class JudgmentGateError(ValueError):
    """议会模式在 R0/PeerBrief/BeliefUpdate 就绪前试图提交研判。"""


@dataclass
class _Runtime:
    """一次图运行的不可序列化依赖；只由 `profile` 节点初始化。"""

    tool_client: ToolClient | None
    model: BaseChatModel | None
    gateway: ToolGateway | None = None
    # 终局推理模型：启用 thinking、不绑定工具，只用于分析师拿到证据后的
    # 一次深度综合；None 时保持既有行为（零改动）。
    thinking_model: BaseChatModel | None = None
    # 分析师工具只由 profile + gateway 派生，构造后整轮复用（工具本身无状态）。
    analyst_tools: list[BaseTool] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _trace(trace_id: str, node: str, **fields: Any) -> dict[str, Any]:
    return {"trace_id": trace_id, "node": node, **fields}


def _require(state: Mapping[str, Any], key: str, expected: type[StateT]) -> StateT:
    value = state.get(key)
    if not isinstance(value, expected):
        raise ValueError(f"research graph requires {expected.__name__} in state[{key!r}]")
    return value


def _round_of_evidence(item: EvidenceItem) -> int | None:
    value = item.tool_params.get("round_no")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _referenced_evidence_ids(state: Mapping[str, Any]) -> set[str]:
    referenced: set[str] = set()
    for result in state.get("analyst_results", ()):
        if isinstance(result, AnalystResult):
            referenced.update(result.evidence_refs)
    for paper in state.get("submissions", ()):
        if isinstance(paper, PositionPaper):
            referenced.update(paper.evidence_ids)
    for belief in state.get("belief_updates", ()):
        if isinstance(belief, BeliefUpdate):
            referenced.update(belief.evidence_ids)
    for rebuttal in state.get("rebuttals", ()):
        if isinstance(rebuttal, Rebuttal):
            referenced.update(rebuttal.evidence_ids)
    for event in state.get("revision_events", ()):
        if isinstance(event, RevisionEvent):
            referenced.update(event.triggering_evidence_ids)
    return referenced


def _assert_council_ready(state: Mapping[str, Any]) -> None:
    submissions = [
        paper
        for paper in state.get("submissions", ())
        if isinstance(paper, PositionPaper)
    ]
    if not any(paper.position_id.startswith("pos_r00") for paper in submissions):
        raise JudgmentGateError(
            "council judgment requires R0 independent positions before commit"
        )
    briefs = [
        brief
        for brief in state.get("peer_briefs", ())
        if isinstance(brief, PeerBrief)
    ]
    if not any(brief.deliberation_round >= 1 for brief in briefs):
        raise JudgmentGateError(
            "council judgment requires R1 peer briefs before commit"
        )
    updates = [
        update
        for update in state.get("belief_updates", ())
        if isinstance(update, BeliefUpdate)
    ]
    if not any(update.deliberation_round >= 1 for update in updates):
        raise JudgmentGateError(
            "council judgment requires R1 belief updates before commit"
        )


def commit_judgment(state: Mapping[str, Any]) -> V4Judgment:
    """唯一写者：把研究/议事结果落成 `V4Judgment`。

    - 引用证据必须全部存在于 `EvidenceLedger`；
    - 议会/红队模式必须已有 R0 独立立场与 R1 PeerBrief + BeliefUpdate；
    - `status` 只由 `decision` 推导，`stop_reason` 独立取自运行状态，二者不互相替代。
    """
    request = _require(state, "request", ResearchRequest)
    ledger = _require(state, "evidence_ledger", EvidenceLedger)
    hypotheses = list(state.get("hypotheses", ()))
    collaboration = state.get("collaboration_decision")
    council = state.get("council_decision")

    if (
        isinstance(collaboration, CollaborationDecision)
        and collaboration.mode in _DELIBERATION_MODES
    ):
        _assert_council_ready(state)

    ledger_ids = {item.evidence_id for item in ledger.items}
    referenced = _referenced_evidence_ids(state)
    missing = sorted(referenced - ledger_ids)
    if missing:
        raise JudgmentEvidenceError(
            "judgment references evidence outside the frozen ledger: "
            + ", ".join(missing)
        )

    ranking = sorted(hypotheses, key=lambda item: (-item.prior_confidence, item.ref))
    if isinstance(council, CouncilDecision):
        selected = list(council.selected_hypothesis_refs)
        rejected = list(council.rejected_hypothesis_refs)
        minority = list(council.minority_opinions)
        invalidators = list(council.invalidators)
        decision: Literal["accept", "revise", "insufficient", "escalate"] = (
            council.decision
        )
        confidence = council.confidence
    else:
        selected = [ranking[0].ref] if ranking else []
        rejected = [hypothesis.ref for hypothesis in ranking[1:]]
        minority = []
        invalidators = sorted(
            {
                disconfirmer
                for hypothesis in ranking
                if hypothesis.ref in selected
                for disconfirmer in hypothesis.disconfirmers
            }
        )
        passed = bool(state.get("sufficiency_passed", False))
        decision = "accept" if passed else "insufficient"
        confidence = ranking[0].prior_confidence if ranking else 0.0

    status: Literal["ok", "partial", "insufficient"]
    if decision == "accept":
        status = "ok"
    elif decision == "revise":
        status = "partial"
    else:
        status = "insufficient"

    stop_reason = str(
        state.get("stop_reason")
        or ("sufficient" if state.get("sufficiency_passed") else "insufficient")
    )
    open_gaps = sorted(
        gap.gap_id
        for gap in state.get("research_gaps", ())
        if isinstance(gap, ResearchGap) and gap.status in {"open", "researching"}
    )
    round_no = int(state.get("research_round_no", 0))

    return V4Judgment(
        judgment_id=f"jdg_{request.run_id}_{round_no:02d}",
        run_id=request.run_id,
        profile=request.profile,
        subject_type=request.subject_type,
        subject_code=request.subject_code,
        horizon=request.horizon,
        as_of=request.as_of,
        data_cutoff=request.data_cutoff,
        snapshot_id=request.snapshot_id,
        decision=decision,
        status=status,
        stop_reason=stop_reason,
        selected_hypothesis_refs=selected,
        rejected_hypothesis_refs=rejected,
        minority_opinions=minority,
        invalidators=invalidators,
        evidence_ids=sorted(ledger_ids | referenced),
        unresolved_question_ids=open_gaps,
        confidence=confidence,
        parent_judgment_ids=list(state.get("parent_judgment_ids", ())),
        relation=None,
        config_version=request.config_version,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        method_version=request.method_version,
        created_at=request.as_of,
    )


def _deterministic_evidence(task: ResearchTask) -> EvidenceItem:
    """无工具客户端时的离线证据桩，保证测试/回放不触网。"""
    tool_group = task.get("tool_group", "market_snapshot")
    round_no = int(task.get("round_no", 0))
    hypothesis_ref = task.get("hypothesis_ref", "")
    digest = canonical_digest(
        {"tool": tool_group, "round": round_no, "hypothesis": hypothesis_ref}
    )
    as_of = _as_datetime(task.get("as_of"))
    data_cutoff = _as_datetime(task.get("data_cutoff"))
    return EvidenceItem(
        evidence_id=f"ev_{digest[:24]}",
        source="offline-fixture",
        source_type=tool_group,
        event_time=data_cutoff,
        published_at=as_of,
        fetched_at=as_of,
        as_of=as_of,
        evidence_kind="direct",
        supports_hypothesis_refs=[hypothesis_ref] if hypothesis_ref else [],
        data_quality="high",
        time_confidence="high",
        tool=tool_group,
        tool_params={"round_no": round_no, "tool": tool_group},
        response_digest=digest,
        snapshot_id=task.get("snapshot_id", ""),
    )


def _deterministic_analyst(
    task: AnalystTask, profile: RunProfile
) -> AnalystResult:
    """无模型时的离线分析师桩：只复述证据覆盖，不写规范图。"""
    role = task.get("role", "lead_thesis")
    round_no = int(task.get("round_no", 0))
    evidence_ids = list(task.get("evidence_ids", ()))
    hypothesis_refs = list(task.get("hypothesis_refs", ()))
    return AnalystResult(
        result_id=f"analyst_r{round_no:02d}_{role}",
        role=role,
        research_round=round_no,
        claim=(
            f"{role} reviewed {len(evidence_ids)} evidence items for "
            + "/".join(hypothesis_refs)
        ),
        evidence_refs=evidence_ids,
        confidence=0.5,
        steps_used=0,
        max_steps=profile.analyst_max_steps,
    )


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raise ValueError("research task requires datetime boundaries from state")


def _whitelisted_task_params(tool: str, task: ResearchTask) -> dict[str, Any]:
    """构造原子工具的工具参数：只保留 TOOL_MAP 白名单内可推导的键。

    为什么不能沿用 ``round_no/subject_code/tool``：那些是研究编排字段，
    不是工具契约参数；``NodeToolClient`` 会在发请求前以
    ``unexpected params`` 拒绝。日期一律取 `data_cutoff`，绝不读系统时间。
    """
    spec = TOOL_MAP.get(tool)
    if spec is None:
        return {}
    cutoff = _as_datetime(task.get("data_cutoff"))
    subject_code = str(task.get("subject_code", ""))
    candidates: dict[str, Any] = {
        "date": cutoff.date().isoformat(),
        "symbol": subject_code,
        "ts_code": subject_code,
    }
    return {
        name: candidates[name]
        for name in spec.allowed_params
        if name in candidates and candidates[name]
    }


def build_research_graph(
    *,
    tool_client: ToolClient | None = None,
    analyst_model: BaseChatModel | None = None,
    thinking_model: BaseChatModel | None = None,
) -> CompiledStateGraph[
    ResearchGraphState, None, ResearchGraphState, ResearchGraphState
]:
    """组装并编译 V4 研究主图。

    `tool_client` 与 `analyst_model` 必须由调用方注入；缺省时图使用确定性
    离线桩（不触网、不读 API key），仅用于拓扑验证与回放。
    `thinking_model` 可选：启用后会为每个分析师追加一次不带工具的深度综合。
    """
    runtime = _Runtime(
        tool_client=tool_client,
        model=analyst_model,
        thinking_model=thinking_model,
    )
    council_graph = build_council_graph()

    def profile_node(state: ResearchGraphState) -> dict[str, Any]:
        request = _require(state, "request", ResearchRequest)
        profile = get_profile(request.profile)
        if runtime.tool_client is not None:
            runtime.gateway = ToolGateway(
                runtime.tool_client,
                as_of=request.as_of,
                allowed_tools=analyst_tool_groups(profile),
                clock=lambda: request.as_of,
            )
            # 工具名/参数只来自 profile 与 TOOL_MAP，构造阶段不触网。
            runtime.analyst_tools = build_analyst_tools(profile, runtime.gateway)
        case_file = state.get("case_file")
        case_version = (
            case_file.case_version if isinstance(case_file, CaseFile) else 1
        )
        return {
            "research_round_no": int(state.get("research_round_no", 0)),
            "deliberation_round_no": int(state.get("deliberation_round_no", 0)),
            "case_version": case_version,
            "usage": [],
            "errors": [],
            "trace": [
                _trace("trace_profile", "profile", profile=profile.name)
            ],
        }

    def plan_node(state: ResearchGraphState) -> dict[str, Any]:
        request = _require(state, "request", ResearchRequest)
        profile = get_profile(request.profile)
        round_no = int(state.get("research_round_no", 0))
        plan = build_plan(
            request, list(state.get("hypotheses", ())), profile, round_no
        )
        return {
            "plan": plan,
            "plan_id": plan.plan_id,
            "trace": [
                _trace(
                    f"trace_plan_r{round_no:02d}",
                    "plan",
                    round_no=round_no,
                    plan_id=plan.plan_id,
                )
            ],
        }

    def dispatch_node(state: ResearchGraphState) -> dict[str, Any]:
        # 纯占位节点：真正的扇出由 `dispatch_research` 条件边完成。
        return {}

    def dispatch_research(state: ResearchGraphState) -> list[Send] | str:
        case_file = state.get("case_file")
        # 简报证据已冻结（decision_type=briefing_claim）：不再派发研究取数任务。
        # 否则工具会产出 ledger 之外的证据（实测午报 analyst 引用
        # ev_*_sector_intraday），commit 时被 JudgmentEvidenceError 拒绝，LLM claim 全丢。
        if isinstance(case_file, CaseFile) and case_file.decision_type == "briefing_claim":
            return "collect"
        request = _require(state, "request", ResearchRequest)
        plan = state.get("plan")
        if not isinstance(plan, ResearchPlan):
            return []
        round_no = int(state.get("research_round_no", 0))
        tasks: list[Send] = []
        for hypothesis_ref in plan.hypothesis_refs:
            for tool_group in plan.tool_groups:
                tasks.append(
                    Send(
                        "research_task",
                        ResearchTask(
                            run_id=request.run_id,
                            subject_code=request.subject_code,
                            round_no=round_no,
                            tool_group=tool_group,
                            hypothesis_ref=hypothesis_ref,
                            as_of=request.as_of,
                            data_cutoff=request.data_cutoff,
                            snapshot_id=request.snapshot_id,
                        ),
                    )
                )
        return tasks

    async def research_task_node(task: ResearchTask) -> dict[str, Any]:
        round_no = int(task.get("round_no", 0))
        tool_group = task.get("tool_group", "")
        hypothesis_ref = task.get("hypothesis_ref", "")
        gateway = runtime.gateway
        if gateway is None:
            item = _deterministic_evidence(task)
            return {
                "evidence_items": [item],
                "usage": [{"key": "tool_calls", "value": 1}],
                "trace": [
                    _trace(
                        f"trace_task_r{round_no:02d}_{tool_group}_{item.evidence_id}",
                        "research_task",
                        round_no=round_no,
                        tool=tool_group,
                        offline=True,
                    )
                ],
            }

        # 工具组只是编排概念；真正能取数的是 TOOL_MAP 登记的原子工具。
        atoms = expand_groups([tool_group])
        unmapped = False
        if not atoms:
            if tool_group.strip().lower() in TOOL_GROUPS:
                # 已知但映射为空（如 fundamental 暂无 Node 端点）：不产生调用，
                # 避免把“必然失败的工具”写进证据账本。
                return {
                    "trace": [
                        _trace(
                            f"trace_task_r{round_no:02d}_{tool_group}_empty",
                            "research_task",
                            round_no=round_no,
                            tool=tool_group,
                            empty_tool_group=True,
                        )
                    ]
                }
            # 无映射：保留原名交给网关记录 not_allowed，并显式标记未映射。
            atoms = [tool_group]
            unmapped = True

        evidence_items: list[EvidenceItem] = []
        errors: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        if unmapped:
            traces.append(
                _trace(
                    f"trace_task_r{round_no:02d}_{tool_group}_unmapped",
                    "research_task",
                    round_no=round_no,
                    tool_group=tool_group,
                    tool_group_unmapped=True,
                )
            )

        for atom in atoms:
            # 只传该原子工具白名单内的参数；编排字段（round/subject/tool…）
            # 一律不进 params，否则 NodeToolClient 会以 unexpected params 拒绝。
            params = _whitelisted_task_params(atom, task)
            trace_prefix = f"trace_task_r{round_no:02d}_{tool_group}_{atom}"
            envelope = await gateway.call(atom, params)

            if envelope.error is not None:
                errors.append(
                    {
                        "error_id": f"err_{envelope.evidence_id}",
                        "node": "research_task",
                        "error_kind": envelope.error,
                        "detail": envelope.error_detail,
                        "tool": atom,
                        "tool_group": tool_group,
                        "round_no": round_no,
                    }
                )
                traces.append(
                    _trace(
                        f"{trace_prefix}_{envelope.evidence_id}",
                        "research_task",
                        round_no=round_no,
                        tool=atom,
                        tool_group=tool_group,
                        error=envelope.error,
                    )
                )
                continue

            data = envelope.data if isinstance(envelope.data, Mapping) else {}
            raw_supports = data.get("supports")
            supports = (
                [str(ref) for ref in raw_supports]
                if isinstance(raw_supports, list)
                else ([hypothesis_ref] if hypothesis_ref else [])
            )
            evidence_items.append(
                EvidenceItem(
                    evidence_id=envelope.evidence_id,
                    source=tool_group,
                    source_type=tool_group,
                    event_time=envelope.event_time
                    or _as_datetime(task.get("data_cutoff")),
                    published_at=envelope.published_at
                    or _as_datetime(task.get("as_of")),
                    fetched_at=envelope.fetched_at,
                    as_of=envelope.as_of,
                    evidence_kind="direct",
                    supports_hypothesis_refs=sorted(set(supports)),
                    data_quality=envelope.quality,
                    time_confidence="high",
                    tool=atom,
                    # 审计字段可以保留 round/tool，以便 assess 按轮次统计新增证据；
                    # 它们不是发给 Node 的工具参数。
                    tool_params={
                        "round_no": round_no,
                        "tool": atom,
                        "tool_group": tool_group,
                    },
                    response_digest=canonical_digest(data),
                    snapshot_id=task.get("snapshot_id", ""),
                )
            )
            traces.append(
                _trace(
                    f"{trace_prefix}_{envelope.evidence_id}",
                    "research_task",
                    round_no=round_no,
                    tool=atom,
                    tool_group=tool_group,
                )
            )

        result: dict[str, Any] = {
            "usage": [{"key": "tool_calls", "value": len(atoms)}],
            "trace": traces,
        }
        if evidence_items:
            result["evidence_items"] = evidence_items
        if errors:
            result["errors"] = errors
        return result

    def collect_node(state: ResearchGraphState) -> dict[str, Any]:
        request = _require(state, "request", ResearchRequest)
        ledger = state.get("evidence_ledger")
        if not isinstance(ledger, EvidenceLedger):
            ledger = EvidenceLedger(
                run_id=request.run_id,
                snapshot_id=request.snapshot_id,
                as_of=request.as_of,
                items=[],
                conflict_ids=[],
            )
        incoming = list(state.get("evidence_items", ()))
        merged = merge_by_id(
            [item.model_dump() for item in ledger.items],
            [item.model_dump() for item in incoming],
            "evidence_id",
        )
        items = sorted(
            (EvidenceItem.model_validate(record) for record in merged.items),
            key=lambda item: item.evidence_id,
        )
        new_ledger = EvidenceLedger(
            run_id=ledger.run_id,
            snapshot_id=ledger.snapshot_id,
            as_of=ledger.as_of,
            items=items,
            conflict_ids=sorted(ledger.conflict_ids),
        )
        round_no = int(state.get("research_round_no", 0))
        return {
            "evidence_ledger": new_ledger,
            "trace": [
                _trace(
                    f"trace_collect_r{round_no:02d}",
                    "collect",
                    round_no=round_no,
                    evidence_count=len(items),
                )
            ],
        }

    def analyze_node(state: ResearchGraphState) -> dict[str, Any]:
        round_no = int(state.get("research_round_no", 0))
        return {
            "trace": [
                _trace(
                    f"trace_analyze_r{round_no:02d}",
                    "analyze",
                    round_no=round_no,
                )
            ]
        }

    def dispatch_analysis(state: ResearchGraphState) -> list[Send]:
        request = _require(state, "request", ResearchRequest)
        profile = get_profile(request.profile)
        ledger = state.get("evidence_ledger")
        evidence_ids = (
            sorted(item.evidence_id for item in ledger.items)
            if isinstance(ledger, EvidenceLedger)
            else []
        )
        hypotheses = list(state.get("hypotheses", ()))
        hypothesis_refs = sorted(hypothesis.ref for hypothesis in hypotheses)
        round_no = int(state.get("research_round_no", 0))
        # run 级累计预算：先看是否还够再开一整轮。预算不足以支撑新一轮时不再
        # 创建分析师任务，直接把当前状态送进 assess 收敛（不新增/不改变节点与
        # 边，只切换既有条件边的目标），随后 assess 记 budget 停止。
        if not _budget_allows_new_round(state, profile):
            return [Send("assess", dict(state))]
        remaining = max(
            0, int(profile.token_budget - _analyst_tokens_used(state))
        )
        # 席位是并行扇出的：若每个席位都拿完整 remaining，一轮实际消耗会是
        # remaining × 席位数（实测 3.2 万预算跑出 8 万 token）。按「最低可用
        # 预算」裁剪席位数后再均分，避免 5 席位各 4800 全部首调用即耗尽。
        seats, per_seat_budget = _plan_seats(analyst_seats(profile), remaining)
        # 任务文本必须带假设陈述与证据摘要：只给 ID 会逼模型自己调工具取数，
        # 工具一失败就耗尽步数（实测 analyst_stalled:step_limit ×4）。
        sources = state.get("evidence_sources")
        base_task = build_analyst_task(
            mode="research",
            role=", ".join(seats) or "lead",
            round_no=round_no,
            subject=request.subject_code,
            goal=request.research_goal,
            hypotheses=hypotheses,
            evidence=list(ledger.items) if isinstance(ledger, EvidenceLedger) else [],
            sources=sources if isinstance(sources, Mapping) else None,
        )
        return [
            Send(
                "analyst_seat",
                AnalystTask(
                    role=role,
                    round_no=round_no,
                    profile_name=profile.name,
                    subject_code=request.subject_code,
                    hypothesis_refs=hypothesis_refs,
                    evidence_ids=evidence_ids,
                    task_text=f"role={role}\n{base_task}",
                    token_budget=per_seat_budget,
                ),
            )
            for role in seats
        ]

    async def analyst_seat_node(task: AnalystTask) -> dict[str, Any]:
        profile = get_profile(task.get("profile_name", ""))
        role = task.get("role", "")
        round_no = int(task.get("round_no", 0))
        # run 级剩余预算由 dispatch 下发；缺失时回退到 profile 默认（离线/旧调用）。
        raw_budget = task.get("token_budget")
        token_budget = (
            profile.token_budget if raw_budget is None else int(raw_budget)
        )
        if runtime.model is None:
            result = _deterministic_analyst(task, profile)
        else:
            # 取数已由 research_task 完成；分析师只负责解释证据，因此不再持有
            # 取数工具，步数也收敛到 3，避免"工具越多、越容易耗尽步数"。
            result = await run_analyst_async(
                role=role,
                profile=profile,
                model=runtime.model,
                task=task.get("task_text", ""),
                tools=(),
                max_steps=min(profile.analyst_max_steps, _ANALYST_MAX_STEPS),
                research_round=round_no,
                token_budget=token_budget,
                timeout_s=profile.timeout_s,
                thinking_model=runtime.thinking_model,
            )
        return {
            "analyst_results": [result],
            "usage": [
                {"key": "analyst_steps", "value": result.steps_used},
                {"key": "analyst_tokens", "value": result.tokens_used},
            ],
        }

    def assess_node(state: ResearchGraphState) -> Command[str]:
        request = _require(state, "request", ResearchRequest)
        profile = get_profile(request.profile)
        ledger = _require(state, "evidence_ledger", EvidenceLedger)
        case_file = state.get("case_file")
        issue_id = (
            case_file.open_issue_ids[0]
            if isinstance(case_file, CaseFile) and case_file.open_issue_ids
            else f"issue_{request.run_id}"
        )
        report = evaluate_sufficiency(
            run_id=request.run_id,
            issue_id=issue_id,
            as_of=request.as_of,
            data_cutoff=request.data_cutoff,
            hypotheses=list(state.get("hypotheses", ())),
            evidence=list(ledger.items),
            causal_edges=list(state.get("causal_graph", ())),
        )
        round_no = int(state.get("research_round_no", 0))
        round_evidence = sorted(
            item.evidence_id
            for item in ledger.items
            if _round_of_evidence(item) == round_no
        )
        prior_ledgers = list(state.get("round_ledgers", ()))
        next_round = round_no + 1
        # 预算不足以再开一轮时不得继续 research，否则会无限回到 plan→dispatch。
        can_continue = (
            not report.passed
            and next_round < profile.max_research_rounds
            and _budget_allows_new_round(state, profile)
        )
        round_ledger = RoundLedger(
            round_no=round_no,
            new_evidence_ids=round_evidence,
            changed_hypothesis_ids=[],
            unresolved_gaps=sorted(gap.gap_id for gap in report.gaps),
            contradictions=[],
            next_action="research" if can_continue else "collaborate",
            progress_score=report.scores.evidence_coverage,
            stall_count=0 if round_evidence else len(prior_ledgers),
            revision_budget_left=max(
                0,
                profile.max_revisions - len(state.get("revision_events", ())),
            ),
            stop_reason="sufficient" if report.passed else None,
            sufficiency_passed=report.passed,
        )
        update: dict[str, Any] = {
            "research_gaps": report.gaps,
            "round_ledgers": [*prior_ledgers, round_ledger],
            "sufficiency_passed": report.passed,
            "run_status": "researching",
            "trace": [
                _trace(
                    f"trace_assess_r{round_no:02d}",
                    "assess",
                    plan_id=str(state.get("plan_id", "")),
                    ledger_type="research",
                    round_no=round_no,
                    gaps=[gap.gap_id for gap in report.gaps],
                )
            ],
        }
        if can_continue:
            update["research_round_no"] = next_round
            return Command(goto="plan", update=update)

        update["stop_reason"] = (
            "sufficient" if report.passed else "research_budget_exhausted"
        )
        return Command(goto="collaboration_router", update=update)

    def collaboration_router_node(state: ResearchGraphState) -> dict[str, Any]:
        request = _require(state, "request", ResearchRequest)
        profile = get_profile(request.profile)
        case_file = _require(state, "case_file", CaseFile)
        ledger = _require(state, "evidence_ledger", EvidenceLedger)
        hypotheses = list(state.get("hypotheses", ()))
        scores = sorted(
            (hypothesis.prior_confidence for hypothesis in hypotheses),
            reverse=True,
        )
        conflict_score = 1.0 if ledger.conflict_ids else 0.0
        open_gaps = [
            gap
            for gap in state.get("research_gaps", ())
            if isinstance(gap, ResearchGap) and gap.status in {"open", "researching"}
        ]
        decision = route_collaboration(
            profile=request.profile,
            materiality=case_file.materiality,
            hypothesis_scores=scores,
            evidence_conflict_score=conflict_score,
            cross_layer_conflict=bool(ledger.conflict_ids),
            remaining_budget=profile.token_budget,
            professional_gaps=len(open_gaps),
            publish_ready=bool(state.get("publish_ready", False)),
            counter_evidence_coverage=float(
                state.get("counter_evidence_coverage", 1.0)
            ),
        )
        return {
            "collaboration_decision": decision,
            "trace": [
                _trace(
                    "trace_collaboration_router",
                    "collaboration_router",
                    mode=decision.mode,
                    trigger_codes=decision.trigger_codes,
                )
            ],
        }

    def route_mode(state: ResearchGraphState) -> str:
        decision = state.get("collaboration_decision")
        mode = (
            decision.mode
            if isinstance(decision, CollaborationDecision)
            else "SOLO_DEEP"
        )
        return {
            "SOLO_DEEP": "solo",
            "LEAD_WITH_CONSULTANTS": "consult",
            "ANALYST_COUNCIL": "council",
            "MANDATORY_RED_TEAM": "red_team",
        }[mode]

    async def solo_node(state: ResearchGraphState) -> dict[str, Any]:
        request = _require(state, "request", ResearchRequest)
        # 确定性路径必须与旧实现逐字节一致：无模型时不派发分析师、不加 usage，
        # 保证全部既有离线图测试零改动通过。
        if runtime.model is None:
            return {
                "run_status": "solo",
                "trace": [_trace("trace_solo", "solo")],
            }

        # SOLO_DEEP 也需要一个主分析师完成结构化研判，否则该路径一次 LLM
        # 都不会被调用，无法审计 V4 的智能度。席位取 profile 第一个分析师
        # （单人模式回落到 lead），任务文本只描述真实假设与证据，不含结论。
        profile = get_profile(request.profile)
        # run 级预算不足以再开一轮时，不再派发主分析师，避免预算耗尽后仍新增
        # 模型调用（与 research 席位同一判定口径）。
        if not _budget_allows_new_round(state, profile):
            return {
                "run_status": "solo",
                "trace": [_trace("trace_solo", "solo", budget_skipped=True)],
            }
        remaining = max(
            0, int(profile.token_budget - _analyst_tokens_used(state))
        )
        seats = analyst_seats(profile)
        role = seats[0] if seats else "lead"
        round_no = int(state.get("research_round_no", 0))
        ledger = state.get("evidence_ledger")
        hypotheses = [
            item for item in state.get("hypotheses", ()) if isinstance(item, Hypothesis)
        ]
        sources = state.get("evidence_sources")
        task_text = build_analyst_task(
            mode="SOLO_DEEP",
            role=role,
            round_no=round_no,
            subject=request.subject_code,
            goal=request.research_goal,
            hypotheses=hypotheses,
            evidence=list(ledger.items) if isinstance(ledger, EvidenceLedger) else [],
            sources=sources if isinstance(sources, Mapping) else None,
        )
        try:
            result = await run_analyst_async(
                role=role,
                profile=profile,
                model=runtime.model,
                task=task_text,
                # 取数由 research_task 完成，分析师只解释证据，不持有工具。
                tools=(),
                max_steps=min(profile.analyst_max_steps, _ANALYST_MAX_STEPS),
                research_round=round_no,
                token_budget=remaining,
                timeout_s=profile.timeout_s,
                thinking_model=runtime.thinking_model,
            )
        except Exception as exc:  # noqa: BLE001 - 分析师失败不得让图崩溃
            return {
                "run_status": "solo",
                "analyst_results": [],
                "usage": [{"key": "analyst_error", "value": 1}],
                "trace": [
                    _trace(
                        "trace_solo",
                        "solo",
                        analyst_failed=True,
                        error_kind=type(exc).__name__,
                        detail=str(exc),
                    )
                ],
            }
        return {
            "run_status": "solo",
            "analyst_results": [result],
            "usage": [
                {"key": "analyst_steps", "value": result.steps_used},
                {"key": "analyst_tokens", "value": result.tokens_used},
            ],
            "trace": [
                _trace(
                    "trace_solo",
                    "solo",
                    analyst_role=role,
                    analyst_result_id=result.result_id,
                    analyst_status=result.status,
                    analyst_tokens=result.tokens_used,
                )
            ],
        }

    def consult_node(state: ResearchGraphState) -> dict[str, Any]:
        decision = state.get("collaboration_decision")
        roles = (
            list(decision.selected_roles)
            if isinstance(decision, CollaborationDecision)
            else []
        )
        return {
            "run_status": "consult",
            "trace": [
                _trace("trace_consult", "consult", selected_roles=roles)
            ],
        }

    async def _council_update(state: ResearchGraphState) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_file": state.get("case_file"),
            "evidence_ledger": state.get("evidence_ledger"),
            "hypotheses": list(state.get("hypotheses", ())),
            "causal_graph": list(state.get("causal_graph", ())),
            "research_gaps": list(state.get("research_gaps", ())),
            "collaboration_decision": state.get("collaboration_decision"),
            "conflicts": [],
        }
        result = await council_graph.ainvoke(
            cast(DeliberationState, payload),
            config=cast(RunnableConfig, {"recursion_limit": 100}),
        )
        update: dict[str, Any] = {
            "trace": [
                _trace(
                    "trace_council",
                    "council",
                    ledger_type="deliberation",
                    deliberation_round=max(
                        (
                            ledger.deliberation_round
                            for ledger in result.get(
                                "deliberation_round_ledgers", ()
                            )
                        ),
                        default=0,
                    ),
                )
            ]
        }
        for key in (
            "submissions",
            "objections",
            "rebuttals",
            "peer_briefs",
            "belief_updates",
            "influence_edges",
            "revision_events",
            "deliberation_round_ledgers",
            "usage",
        ):
            if key in result:
                update[key] = result[key]
        ledgers = list(result.get("deliberation_round_ledgers", ()))
        update["deliberation_round_no"] = max(
            (ledger.deliberation_round for ledger in ledgers), default=0
        )
        if "council_decision" in result:
            update["council_decision"] = result["council_decision"]
        if "evidence_ledger" in result:
            update["evidence_ledger"] = result["evidence_ledger"]
        if "case_version" in result:
            update["case_version"] = result["case_version"]
        if result.get("run_status") is not None:
            update["run_status"] = result["run_status"]
        if result.get("stop_reason") is not None:
            update["stop_reason"] = result["stop_reason"]
        return update

    async def council_node(state: ResearchGraphState) -> dict[str, Any]:
        return await _council_update(state)

    async def red_team_node(state: ResearchGraphState) -> dict[str, Any]:
        return await _council_update(state)

    def route_after_collaboration(state: ResearchGraphState) -> str:
        decision = state.get("council_decision")
        if isinstance(decision, CouncilDecision) and decision.decision == "revise":
            return "revise"
        return "decision_gate"

    def revise_node(state: ResearchGraphState) -> dict[str, Any]:
        return {
            "run_status": "revised",
            "trace": [_trace("trace_revise", "revise")],
        }

    def decision_gate_node(state: ResearchGraphState) -> dict[str, Any]:
        judgment = commit_judgment(cast(Mapping[str, Any], state))
        return {
            "judgment_draft": judgment,
            "run_status": judgment.status,
            "stop_reason": judgment.stop_reason,
            "trace": [
                _trace(
                    "trace_decision_gate",
                    "decision_gate",
                    judgment_id=judgment.judgment_id,
                    decision=judgment.decision,
                    status=judgment.status,
                )
            ],
        }

    builder: StateGraph[
        ResearchGraphState, None, ResearchGraphState, ResearchGraphState
    ] = StateGraph(ResearchGraphState)

    builder.add_node("profile", profile_node)
    builder.add_node("plan", plan_node)
    builder.add_node("dispatch", dispatch_node)
    builder.add_node(
        "research_task",
        research_task_node,  # type: ignore[arg-type]
        input_schema=ResearchTask,
    )
    builder.add_node("collect", collect_node)
    builder.add_node("analyze", analyze_node)
    builder.add_node(
        "analyst_seat",
        analyst_seat_node,  # type: ignore[arg-type]
        input_schema=AnalystTask,
    )
    builder.add_node("assess", assess_node)
    builder.add_node("collaboration_router", collaboration_router_node)
    builder.add_node("solo", solo_node)
    builder.add_node("consult", consult_node)
    builder.add_node("council", council_node)
    builder.add_node("red_team", red_team_node)
    builder.add_node("revise", revise_node)
    builder.add_node("decision_gate", decision_gate_node)

    builder.add_edge(START, "profile")
    builder.add_edge("profile", "plan")
    builder.add_edge("plan", "dispatch")
    builder.add_conditional_edges(
        "dispatch", dispatch_research, ["collect", "research_task"]
    )
    builder.add_edge("research_task", "collect")
    builder.add_edge("collect", "analyze")
    builder.add_conditional_edges(
        "analyze", dispatch_analysis, ["analyst_seat", "assess"]
    )
    builder.add_edge("analyst_seat", "assess")
    builder.add_conditional_edges(
        "collaboration_router",
        route_mode,
        ["solo", "consult", "council", "red_team"],
    )
    builder.add_edge("solo", "decision_gate")
    builder.add_edge("consult", "decision_gate")
    builder.add_edge("red_team", "decision_gate")
    builder.add_conditional_edges(
        "council", route_after_collaboration, ["revise", "decision_gate"]
    )
    builder.add_edge("revise", "decision_gate")
    builder.add_edge("decision_gate", END)

    return builder.compile()
