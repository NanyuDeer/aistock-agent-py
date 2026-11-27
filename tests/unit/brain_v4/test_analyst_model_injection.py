"""V4 真实模型与工具注入测试（P2-live-model）。

全部离线：模型是确定性假 `BaseChatModel`，工具客户端是内存假 `ToolClient`，
不构造 ChatOpenAI、不触网、不读系统时间。覆盖：

- profile → quick/deep 模型选择（monkeypatch 替身，不真正建客户端）；
- `profile.tool_groups` → StructuredTool，工具名与 `TOOL_MAP` 一致；
- 工具调用真实经过 `ToolGateway`，参数与 `as_of` 进入 provenance；
- 把假模型注入主图后 `ainvoke` 能完成一次研究循环并确实使用注入模型；
- 超 token 预算 → 结构化 `budget_exhausted`；超时 → 结构化 `timeout`。
"""

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from aistock_agent.brain_v4.analysts.kernel import (
    AnalystResult,
    run_analyst_async,
)
from aistock_agent.brain_v4.analysts.model_factory import build_analyst_model
from aistock_agent.brain_v4.analysts.tools import build_analyst_tools
from aistock_agent.brain_v4.contracts.deliberation import CaseFile
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchRequest,
)
from aistock_agent.brain_v4.graph.builder import build_research_graph
from aistock_agent.brain_v4.graph.profiles import RunProfile, get_profile
from aistock_agent.brain_v4.tools.gateway import ToolClientResult, ToolGateway

AS_OF = datetime.fromisoformat("2026-11-27T10:00:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-27T09:30:00+08:00")
EVENT_TIME = datetime.fromisoformat("2026-11-27T09:00:00+08:00")
PUBLISHED_AT = datetime.fromisoformat("2026-11-27T09:05:00+08:00")
FIXED_NOW = datetime.fromisoformat("2026-11-27T10:00:05+08:00")
VALID_TIME = datetime.fromisoformat("2026-11-27T09:00:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-27T15:00:00+08:00")

LIQUIDITY_REF = "hyp_liquidity:v1"
RISK_REF = "hyp_risk_pref:v1"


# ---------------------------------------------------------------------------
# 模型工厂：只验证选择逻辑，用替身替代真实 LLM 客户端
# ---------------------------------------------------------------------------


def test_alert_hot_selects_quick_model(monkeypatch: pytest.MonkeyPatch) -> None:
    quick_sentinel = object()
    deep_sentinel = object()
    quick_calls: list[dict[str, object]] = []
    deep_calls: list[dict[str, object]] = []

    def fake_quick(**kwargs: object) -> object:
        quick_calls.append(kwargs)
        return quick_sentinel

    def fake_deep(**kwargs: object) -> object:
        deep_calls.append(kwargs)
        return deep_sentinel

    monkeypatch.setattr(
        "aistock_agent.brain_v4.analysts.model_factory.get_quick_think", fake_quick
    )
    monkeypatch.setattr(
        "aistock_agent.brain_v4.analysts.model_factory.get_deep_think", fake_deep
    )

    model = build_analyst_model(get_profile("alert_hot"))

    assert model is quick_sentinel
    assert quick_calls == [{"temperature": 0}]
    assert deep_calls == []


@pytest.mark.parametrize(
    "profile_name", ["market_trace", "briefing_morning", "briefing_midday", "briefing_evening"]
)
def test_deep_profiles_select_deep_model(
    monkeypatch: pytest.MonkeyPatch, profile_name: str
) -> None:
    quick_sentinel = object()
    deep_sentinel = object()
    quick_calls: list[dict[str, object]] = []
    deep_calls: list[dict[str, object]] = []

    def fake_quick(**kwargs: object) -> object:
        quick_calls.append(kwargs)
        return quick_sentinel

    def fake_deep(**kwargs: object) -> object:
        deep_calls.append(kwargs)
        return deep_sentinel

    monkeypatch.setattr(
        "aistock_agent.brain_v4.analysts.model_factory.get_quick_think", fake_quick
    )
    monkeypatch.setattr(
        "aistock_agent.brain_v4.analysts.model_factory.get_deep_think", fake_deep
    )

    model = build_analyst_model(get_profile(profile_name))

    assert model is deep_sentinel
    assert deep_calls == [{"temperature": 0}]
    assert quick_calls == []


# ---------------------------------------------------------------------------
# 工具暴露：只暴露 profile.tool_groups 中登记在 TOOL_MAP 的工具
# ---------------------------------------------------------------------------


def _profile_with(tool_groups: list[str]) -> RunProfile:
    # 借用不可变 profile 复刻一份受限工具组，验证“只按 profile 生成工具”。
    return get_profile("market_trace").model_copy(update={"tool_groups": tool_groups})


def test_tools_only_cover_profile_groups_registered_in_tool_map() -> None:
    profile = _profile_with(["market_snapshot", "quote", "event_evidence"])
    gateway = ToolGateway(
        _RecordingClient(), as_of=AS_OF, allowed_tools=profile.tool_groups
    )

    tools = build_analyst_tools(profile, gateway)

    names = {tool.name for tool in tools}
    # event_evidence 只在网关 allowlist 中，未登记到 TOOL_MAP，无法真实取数 → 不生成。
    assert names == {"market_snapshot", "quote"}
    assert names <= set(profile.tool_groups)
    assert all(isinstance(tool, BaseTool) for tool in tools)


def test_alert_hot_has_no_tool_when_groups_are_not_in_tool_map() -> None:
    profile = get_profile("alert_hot")
    gateway = ToolGateway(
        _RecordingClient(), as_of=AS_OF, allowed_tools=profile.tool_groups
    )

    assert build_analyst_tools(profile, gateway) == []


class _RecordingClient:
    """内存假 ToolClient：记录调用参数并返回固定数据。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, dict(params)))
        return ToolClientResult(
            data={"ok": True, "tool": tool},
            published_at=PUBLISHED_AT,
            event_time=EVENT_TIME,
            quality="high",
        )


@pytest.mark.asyncio
async def test_tool_call_goes_through_gateway_with_params_and_as_of() -> None:
    client = _RecordingClient()
    profile = _profile_with(["market_snapshot"])
    gateway = ToolGateway(
        client,
        as_of=AS_OF,
        allowed_tools=profile.tool_groups,
        clock=lambda: FIXED_NOW,
    )

    tool = build_analyst_tools(profile, gateway)[0]
    raw = await tool.ainvoke({"date": "2026-11-27", "slim": True})

    assert client.calls == [
        ("market_snapshot", {"date": "2026-11-27", "slim": True})
    ]
    payload = json.loads(raw)
    assert payload["tool"] == "market_snapshot"
    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["error"] is None
    assert payload["data"]["ok"] is True


# ---------------------------------------------------------------------------
# 主图注入：假模型 + 假客户端完成一次研究循环
# ---------------------------------------------------------------------------


class _RoundAwareClient:
    """按研究轮次返回不同支撑假设的离线客户端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, dict(params)))
        round_no = int(params["round_no"])
        supports = (
            [LIQUIDITY_REF] if round_no == 0 else [LIQUIDITY_REF, RISK_REF]
        )
        return ToolClientResult(
            data={"supports": supports, "round_no": round_no},
            event_time=EVENT_TIME,
            published_at=PUBLISHED_AT,
            quality="high",
        )


class _InjectedAnalystModel(BaseChatModel):
    """确定性假模型：每次直接产出 `AnalystResult` 结构化工具调用。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "injected-analyst-fake"

    def bind_tools(
        self, tools: Sequence[BaseTool | dict[str, Any]], **kwargs: Any
    ) -> "_InjectedAnalystModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystResult",
                                "args": {
                                    "claim": "injected model claim",
                                    "evidence_refs": [],
                                    "confidence": 0.6,
                                    "uncertainties": [],
                                    "requested_gaps": [],
                                },
                                "id": f"call_injected_{self.calls}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req_p2_injection",
        run_id="run_p2_injection",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        research_goal="Which root cause explains today's index move?",
        required_questions=[],
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=DATA_CUTOFF,
        snapshot_id="snap_p2_injection",
        config_version="cfg_v1",
        model_version="model_v1",
        prompt_version="prompt_v1",
        method_version="method_v1",
        budget_policy="market_trace",
        stop_policy="default",
    )


def _case_file() -> CaseFile:
    return CaseFile(
        case_id="case_p2_injection",
        run_id="run_p2_injection",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains today's index move?",
        decision_type="market_root_cause",
        materiality="high",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_p2_injection",
        frozen_fact_ids=[],
        hypothesis_refs=[LIQUIDITY_REF, RISK_REF],
        open_issue_ids=["issue_hyp_liquidity", "issue_hyp_risk_pref"],
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _hypotheses() -> list[Hypothesis]:
    return [
        Hypothesis(
            hypothesis_id="hyp_liquidity",
            version=1,
            statement="流动性收缩是主因",
            status="open",
            prior_confidence=0.56,
            assumptions=["北向资金净流出"],
            disconfirmers=["北向资金转为净流入"],
            scope="market",
        ),
        Hypothesis(
            hypothesis_id="hyp_risk_pref",
            version=1,
            statement="风险偏好回落是主因",
            status="open",
            prior_confidence=0.51,
            assumptions=["高 beta 板块领跌"],
            disconfirmers=["高 beta 板块逆势上涨"],
            scope="market",
        ),
    ]


def _causal_edges() -> list[CausalEdge]:
    return [
        CausalEdge(
            edge_id="edge_liquidity_index",
            from_node="hyp_liquidity",
            to_node="index_move",
            mechanism="流动性收缩压缩估值",
            supporting_evidence_ids=["ev_liquidity_fixture"],
            edge_type="inference",
            confidence=0.55,
            valid_time=VALID_TIME,
            magnitude_check="指数下跌 1.2%",
            counterfactual_check="若北向净流入则指数不应下跌",
        ),
        CausalEdge(
            edge_id="edge_risk_index",
            from_node="hyp_risk_pref",
            to_node="index_move",
            mechanism="风险偏好回落压制高 beta",
            supporting_evidence_ids=["ev_risk_fixture"],
            edge_type="inference",
            confidence=0.5,
            valid_time=VALID_TIME,
            magnitude_check="高 beta 板块下跌 2.1%",
            counterfactual_check="若高 beta 领涨则风险偏好未回落",
        ),
    ]


def _initial_state() -> dict[str, Any]:
    return {
        "request": _request(),
        "case_file": _case_file(),
        "evidence_ledger": EvidenceLedger(
            run_id="run_p2_injection",
            snapshot_id="snap_p2_injection",
            as_of=AS_OF,
            items=[],
            conflict_ids=["conflict_liquidity_vs_risk_pref"],
        ),
        "hypotheses": _hypotheses(),
        "causal_graph": _causal_edges(),
        "research_gaps": [],
        "publish_ready": False,
        "counter_evidence_coverage": 1.0,
        "parent_judgment_ids": [],
        "usage": [],
        "errors": [],
        "trace": [],
    }


@pytest.mark.asyncio
async def test_research_graph_uses_injected_model_and_tool_client() -> None:
    model = _InjectedAnalystModel()
    client = _RoundAwareClient()
    graph = build_research_graph(tool_client=client, analyst_model=model)

    final: dict[str, Any] = await graph.ainvoke(
        _initial_state(), config={"recursion_limit": 200}
    )

    # 注入模型确实被 analyst 席位调用，且结果来自该模型。
    assert model.calls > 0
    results = [item for item in final["analyst_results"] if isinstance(item, AnalystResult)]
    assert results
    assert {item.claim for item in results} == {"injected model claim"}
    assert all(item.status == "ok" for item in results)
    assert all(item.steps_used > 0 for item in results)
    # 注入工具客户端被主图真实调用，产出证据并进入研判。
    assert client.calls
    assert final["judgment_draft"].run_id == "run_p2_injection"


# ---------------------------------------------------------------------------
# 预算与超时：结构化降级，不抛裸异常
# ---------------------------------------------------------------------------


class _TokenHungryModel(BaseChatModel):
    """每次都回报超出预算的 token 用量。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "token-hungry-fake"

    def bind_tools(
        self, tools: Sequence[BaseTool | dict[str, Any]], **kwargs: Any
    ) -> "_TokenHungryModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystResult",
                                "args": {
                                    "claim": "too expensive",
                                    "evidence_refs": [],
                                },
                                "id": f"call_token_{self.calls}",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": 4000,
                            "output_tokens": 2000,
                            "total_tokens": 6000,
                        },
                    )
                )
            ]
        )


@pytest.mark.asyncio
async def test_token_budget_exhaustion_returns_structured_result() -> None:
    profile = get_profile("alert_hot")

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=_TokenHungryModel(),
        task="验证 token 预算",
        token_budget=profile.token_budget,
        timeout_s=5,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "stalled"
    assert result.error_kind == "budget_exhausted"
    assert result.tokens_used >= profile.token_budget
    assert result.token_budget == profile.token_budget


class _SlowModel(BaseChatModel):
    """异步慢模型：用于触发外层超时并验证可取消。"""

    delay: float = 0.2

    @property
    def _llm_type(self) -> str:
        return "slow-analyst-fake"

    def bind_tools(
        self, tools: Sequence[BaseTool | dict[str, Any]], **kwargs: Any
    ) -> "_SlowModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(self.delay)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])


@pytest.mark.asyncio
async def test_timeout_returns_structured_stalled_result() -> None:
    profile = get_profile("alert_hot")

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=_SlowModel(),
        task="验证超时降级",
        timeout_s=0.01,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "stalled"
    assert result.error_kind == "timeout"
