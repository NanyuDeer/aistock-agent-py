"""P6 真实 LLM 研究图测试（全部离线）。

根因 B：旧运行器直接跑写死的确定性脚本，`model` 只写进审计、从不调用 LLM。
本文件用 FakeChatModel（返回结构化 AnalystResult 与真实 usage）+ FakeToolClient
（返回真实形状的行情载荷），验证 `run_research_graph` 确实执行研究图并产出：

- 非空 judgment、证据账本条目与真实 token 用量；
- 竞争假设 statement 来自输入证据，而不是固定文案；
- 同输入两次运行 evidence_ids 一致（确定性）；
- 图执行异常收敛为 `status="degraded"`，不抛裸异常。

不触网、不调用真实 LLM、不读系统时间。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.runtime.graph_run import run_research_graph
from aistock_agent.brain_v4.runtime.inputs import build_market_trace_case
from aistock_agent.brain_v4.tools.gateway import ToolClientResult

pytestmark = pytest.mark.asyncio

CST = timezone(timedelta(hours=8))
TRADING_DATE = date(2026, 11, 27)
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
DATA_CUTOFF = datetime(2026, 11, 27, 15, 0, tzinfo=CST)
DATE_COMPACT = "20261127"


class _ReviewResult:
    def __init__(self, status: str, report: dict[str, object] | None) -> None:
        self.status = status
        self.report = report


class _CaseNodeClient:
    """最小 NodeDataSource：只提供 `build_market_trace_case` 需要的方法。"""

    def __init__(
        self,
        *,
        index_name: str,
        index_change: float,
        sector_name: str,
        sector_change: float,
    ) -> None:
        self._index_name = index_name
        self._index_change = index_change
        self._sector_name = sector_name
        self._sector_change = sector_change

    async def get_close_snapshot(
        self, snapshot_date: str
    ) -> dict[str, object] | None:
        return {
            "status": "complete",
            "trade_date": DATE_COMPACT,
            "captured_at": "2026-11-27T15:10:00+08:00",
            "indexes": {
                "000001.SH": {
                    "ts_code": "000001.SH",
                    "name": self._index_name,
                    "close": 3012.4,
                    "change_pct": self._index_change,
                }
            },
            "breadth": {
                "total_count": 5200,
                "advance_count": 1800,
                "decline_count": 3200,
                "flat_count": 200,
            },
            "turnover": {
                "amount_yuan": 820000000000,
                "previous_amount_yuan": 950000000000,
                "change_pct": -0.137,
            },
            "style": {
                "large_cap_change_pct": -0.4,
                "small_cap_change_pct": -2.1,
            },
            "sectors": {self._sector_name: {"change_pct": self._sector_change}},
        }

    async def get_calendar_events(
        self,
        date_from: str,
        date_to: str,
        *,
        importance: str | None = None,
    ) -> list[dict[str, object]] | None:
        return [
            {
                "event_date": "2026-11-27",
                "event_time": "2026-11-27T07:30:00+08:00",
                "published_at": "2026-11-27T07:45:00+08:00",
                "title": "盘前政策口径",
                "content": "稳增长政策延续",
                "importance": "high",
                "source": "calendar:cn",
            }
        ]

    async def get_review_analysis_report(self, report_date: date) -> _ReviewResult:
        return _ReviewResult("not_found", None)


class FakeToolClient:
    """内存假 ToolClient：返回真实形状的收盘行情载荷。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, dict(params)))
        return ToolClientResult(
            data={"tool": tool, "indexes": {"000001.SH": {"close": 3012.4}}},
            published_at=DATA_CUTOFF,
            event_time=DATA_CUTOFF,
            quality="high",
        )


class _StructuredAnalystModel(BaseChatModel):
    """确定性假模型：直接产出 AnalystResult 工具调用，并回报真实 token 用量。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "p6-structured-analyst-fake"

    def bind_tools(
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _StructuredAnalystModel:
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
                                    "claim": "基于证据的结构化结论",
                                    "evidence_refs": [],
                                    "confidence": 0.6,
                                    "uncertainties": [],
                                    "requested_gaps": [],
                                },
                                "id": f"call_p6_{self.calls}",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": 40,
                            "output_tokens": 20,
                            "total_tokens": 60,
                        },
                    )
                )
            ]
        )


async def _make_case(
    *,
    index_name: str,
    index_change: float,
    sector_name: str,
    sector_change: float,
):
    client = _CaseNodeClient(
        index_name=index_name,
        index_change=index_change,
        sector_name=sector_name,
        sector_change=sector_change,
    )
    inputs = await build_market_trace_case(
        client, AS_OF, DATA_CUTOFF, f"snap_p6_{sector_name}"
    )
    return inputs.case


async def test_run_research_graph_produces_judgment_evidence_and_tokens() -> None:
    case = await _make_case(
        index_name="上证指数",
        index_change=-1.18,
        sector_name="growth",
        sector_change=-2.4,
    )

    outcome = await run_research_graph(
        case, model=_StructuredAnalystModel(), tool_client=FakeToolClient()
    )

    assert outcome.status == "ok"
    assert outcome.judgment is not None
    assert outcome.evidence_ledger is not None
    assert len(outcome.evidence_ledger.items) > 0
    assert int(outcome.usage["total_tokens"]) > 0
    assert outcome.usage["token_source"] != "unavailable"


async def test_hypothesis_statements_come_from_case_evidence() -> None:
    first = await _make_case(
        index_name="上证指数",
        index_change=-1.18,
        sector_name="growth",
        sector_change=-2.4,
    )
    second = await _make_case(
        index_name="创业板指",
        index_change=1.42,
        sector_name="defensive",
        sector_change=0.9,
    )

    outcome_a = await run_research_graph(
        first, model=_StructuredAnalystModel(), tool_client=FakeToolClient()
    )
    outcome_b = await run_research_graph(
        second, model=_StructuredAnalystModel(), tool_client=FakeToolClient()
    )

    statements_a = [hypothesis.statement for hypothesis in outcome_a.hypotheses]
    statements_b = [hypothesis.statement for hypothesis in outcome_b.hypotheses]

    # 假设文本来自输入证据：两个不同 case 的假设集合必然不同。
    assert statements_a != statements_b
    assert any("创业板指" in text for text in statements_b)
    # 不得写死确定性的固定结论文案。
    assert all("流动性是放大器" not in text for text in statements_a + statements_b)


async def test_same_input_is_deterministic_for_evidence_ids() -> None:
    case = await _make_case(
        index_name="上证指数",
        index_change=-1.18,
        sector_name="growth",
        sector_change=-2.4,
    )

    first = await run_research_graph(
        case, model=_StructuredAnalystModel(), tool_client=FakeToolClient()
    )
    second = await run_research_graph(
        case, model=_StructuredAnalystModel(), tool_client=FakeToolClient()
    )

    assert first.judgment is not None
    assert second.judgment is not None
    assert first.judgment.evidence_ids == second.judgment.evidence_ids


async def test_model_failure_returns_degraded_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _make_case(
        index_name="上证指数",
        index_change=-1.18,
        sector_name="growth",
        sector_change=-2.4,
    )

    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("model exploded")

    # 真实模型异常会被内核收敛；这里在同一个模型调用边界注入逃逸异常，
    # 验证上层研究图把任何执行失败都收敛为 degraded 而不是抛裸异常。
    monkeypatch.setattr(
        "aistock_agent.brain_v4.graph.builder.run_analyst_async", _boom
    )

    outcome = await run_research_graph(
        case, model=_StructuredAnalystModel(), tool_client=FakeToolClient()
    )

    assert outcome.status == "degraded"
    assert outcome.judgment is None
    assert outcome.errors
