"""P27：受控按需检索（分析师发现缺口 → 关键词搜索 → 结果入账 → 下一轮可见）。

目标行为（north-star）：检索不是开题一次，而是贯穿分析的可迭代动作；
但必须受控——关键词去重、run/席位配额、结果逐条入账、可审计可回放。

本文件用假模型 + 假工具客户端验证，不触网、不读系统时间。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.graph.profiles import get_profile
from aistock_agent.brain_v4.runtime.graph_run import (
    LiveMarketToolClient,
    run_research_graph,
)
from aistock_agent.brain_v4.runtime.inputs import build_market_trace_case
from aistock_agent.brain_v4.tools.gateway import ToolClientResult
from aistock_agent.brain_v4.tools.tool_map import TOOL_GROUPS, TOOL_MAP, expand_groups

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
DATA_CUTOFF = datetime(2026, 11, 27, 15, 0, tzinfo=CST)


class _ReviewResult:
    def __init__(self, status: str, report: dict[str, object] | None = None) -> None:
        self.status = status
        self.report = report


class _CaseNodeClient:
    async def get_close_snapshot(
        self, snapshot_date: str
    ) -> dict[str, object] | None:
        return {
            "status": "complete",
            "trade_date": "20261127",
            "captured_at": "2026-11-27T15:10:00+08:00",
            "indexes": {
                "000001.SH": {
                    "ts_code": "000001.SH",
                    "name": "上证指数",
                    "close": 3012.4,
                    "change_pct": -1.18,
                }
            },
            "breadth": {
                "total_count": 5200,
                "advance_count": 1800,
                "decline_count": 3200,
                "flat_count": 200,
            },
            "turnover": {"amount_yuan": 820000000000, "change_pct": -0.137},
            "style": {"large_cap_change_pct": -0.4, "small_cap_change_pct": -2.1},
            "sectors": {"growth": {"change_pct": -2.4}},
        }

    async def get_last_close_snapshot(self) -> dict[str, object] | None:
        return None

    async def get_intraday_sectors(
        self, *, include_breadth: bool = True
    ) -> dict[str, object] | None:
        return None

    async def get_calendar_events(
        self,
        date_from: str,
        date_to: str,
        *,
        importance: str | None = None,
    ) -> list[dict[str, object]] | None:
        return []

    async def get_news_telegraph(
        self, date_iso: str, *, limit: int = 200
    ) -> dict[str, object] | None:
        return {"items": []}

    async def get_review_analysis_report(self, report_date: date) -> _ReviewResult:
        return _ReviewResult("not_found")


class _SearchToolClient:
    """假工具客户端：记录每次调用，news_search 返回两条命中。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, dict(params)))
        if tool == "news_search":
            query = str(params.get("query", ""))
            return ToolClientResult(
                data={
                    "results": [
                        {
                            "title": f"{query} 政策信号",
                            "content": f"针对 {query} 的公开报道正文",
                            "url": "https://example.com/a",
                        },
                        {
                            "title": f"{query} 市场解读",
                            "content": f"{query} 的市场影响解读",
                            "url": "https://example.com/b",
                        },
                    ],
                    "provider": "tavily",
                },
                published_at=DATA_CUTOFF,
                event_time=DATA_CUTOFF,
                quality="medium",
            )
        return ToolClientResult(
            data={"tool": tool}, published_at=DATA_CUTOFF, quality="high"
        )


class _GapModel(BaseChatModel):
    """缺口模型：每次调用提出三条关键词缺口，用于验证去重与配额。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "p27-gap-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _GapModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        gaps = [
            f"缺口关键词{self.calls}a",
            f"缺口关键词{self.calls}b",
            f"缺口关键词{self.calls}c",
        ]
        # 所有席位都提同一条，验证跨席位关键词去重。
        gaps.append("国常会 国家队 稳市")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystResult",
                                "args": {
                                    "claim": "需要补充外部事实",
                                    "evidence_refs": [],
                                    "confidence": 0.5,
                                    "uncertainties": ["政策细节未知"],
                                    "requested_gaps": gaps,
                                },
                                "id": f"call_p27_{self.calls}",
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


async def _make_case():
    inputs = await build_market_trace_case(
        _CaseNodeClient(), AS_OF, DATA_CUTOFF, "snap_p27"
    )
    return inputs.case


def test_news_search_is_registered_as_on_demand_tool() -> None:
    assert "news_search" in TOOL_MAP
    assert expand_groups(["news_search"]) == ["news_search"]
    assert TOOL_GROUPS["news_search"] == ("news_search",)
    profile = get_profile("market_trace")
    assert "news_search" in profile.on_demand_tools
    # 预取组不含按需检索工具：research_task 没有 query 参数，绝不能扇出。
    assert "news_search" not in profile.tool_groups


@pytest.mark.asyncio
async def test_gap_search_dedupes_keywords_and_respects_run_quota() -> None:
    case = await _make_case()
    client = _SearchToolClient()

    outcome = await run_research_graph(
        case, model=_GapModel(), tool_client=client
    )

    searches = [
        params["query"]
        for tool, params in client.calls
        if tool == "news_search"
    ]
    # run 级配额 6：即使每席位都提缺口，也绝不能无限检索。
    assert len(searches) == 6
    # 关键词去重：同一席位/跨席位的同一条缺口只检索一次。
    assert len(set(searches)) == len(searches)
    # 网关下发的是分析师原关键词；交易日作用域由 LiveMarketToolClient 统一加。
    assert all("缺口关键词" in query or "国常会" in query for query in searches)

    assert outcome.evidence_ledger is not None
    search_evidence = [
        item
        for item in outcome.evidence_ledger.items
        if item.tool == "news_search"
    ]
    # 每次检索返回 2 条，逐条入账（不是一条汇总证据）。
    assert len(search_evidence) == 12
    assert all(item.data_quality == "medium" for item in search_evidence)
    assert all(item.tool_params["query"] for item in search_evidence)


@pytest.mark.asyncio
async def test_live_client_scopes_search_to_trade_date(monkeypatch) -> None:
    """真实客户端必须把检索限定在证据日，且条数上限受控。"""
    from aistock_agent.services import tavily

    captured: dict[str, object] = {}

    def fake_search(
        query: str, *, topic: str = "news", max_results: int = 5
    ) -> dict[str, object]:
        captured.update(query=query, topic=topic, max_results=max_results)
        return {"results": [], "provider": "fake"}

    monkeypatch.setattr(tavily.TavilyService, "search", staticmethod(fake_search))

    client = LiveMarketToolClient(
        _CaseNodeClient(), as_of=AS_OF, data_cutoff=DATA_CUTOFF, subject_code="000001.SH"
    )
    result = await client.fetch(
        "news_search", {"query": "国常会 国家队 稳市", "max_results": 9}
    )

    assert captured["query"] == "2026-11-27 国常会 国家队 稳市"
    assert captured["topic"] == "news"
    assert captured["max_results"] == 5  # 超过上限被收敛
    assert result.quality == "high"

