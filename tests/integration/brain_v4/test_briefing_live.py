"""P13：早/午/晚报 live 接入分析师内核（全部离线）。

背景：P6/P7 已让 `market_trace` 的 live 路径真正调用研究图，但
`briefing_morning / midday / evening` 的 live 路径仍只做确定性投影，
即便注入模型也从不产生 LLM claim（附录 C.2 要求四试点 profile 的 claim
由分析师内核产出，确定性脚本只作降级路径）。

本文件用 FakeChatModel（返回结构化 AnalystResult 与真实 usage）+ FakeNodeClient
（真实形状的行情载荷）验证：

- 早报 live 在注入模型后产生真实 token 与分析师 claim（llm_claim_count >= 1）；
- 午报保留 parent 链，变化条目携带 parent_judgment_id；
- 晚报按 ID 引用 market_trace 的 judgment，不新增第二套盘后归因；
- 模型缺失时保留确定性投影，写 degraded_reason 且 token 为 0；
- 研究图失败时仍返回结果，status 不为 ok，degraded_reason 非空。

不触网、不调用真实 LLM、不读系统时间。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta, timezone
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.runtime import reset_live_runs, run_live
from aistock_agent.brain_v4.runtime.briefing_live import run_briefing_session
from aistock_agent.brain_v4.runtime.inputs import build_briefing_chain

pytestmark = pytest.mark.asyncio

CST = timezone(timedelta(hours=8))
TRADING_DATE = date(2026, 11, 27)
DATE_ISO = "2026-11-27"
DATE_COMPACT = "20261127"

CLOSE_SNAPSHOT: dict[str, object] = {
    "status": "complete",
    "trade_date": DATE_COMPACT,
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
        "advance_ratio": 0.346,
    },
    "turnover": {
        "amount_yuan": 820000000000,
        "previous_amount_yuan": 950000000000,
        "change_pct": -0.137,
    },
    "style": {"large_cap_change_pct": -0.4, "small_cap_change_pct": -2.1},
    "sectors": {
        "defensive": {"change_pct": 0.6},
        "growth": {"change_pct": -2.4},
    },
}

LAST_CLOSE_SNAPSHOT: dict[str, object] = {
    "status": "complete",
    "trade_date": "20261126",
    "captured_at": "2026-11-26T15:10:00+08:00",
    "indexes": {
        "000001.SH": {
            "ts_code": "000001.SH",
            "name": "上证指数",
            "close": 3046.1,
            "change_pct": -1.02,
        }
    },
    "breadth": {
        "total_count": 5200,
        "advance_count": 2050,
        "decline_count": 2980,
        "flat_count": 170,
        "advance_ratio": 0.394,
    },
    "style": {"large_cap_change_pct": -0.7, "small_cap_change_pct": -1.5},
}

INTRADAY_SECTORS: dict[str, object] = {
    "captured_at": "2026-11-27T12:25:00+08:00",
    "availability": "available",
    "indexes": {
        "000001.SH": {"name": "上证指数", "price": 3020.1, "changePercent": -0.85}
    },
    "breadth": {"advance": 2100, "decline": 2900, "flat": 200},
    "gainers": [{"name": "银行", "changePercent": 0.9}],
    "losers": [{"name": "半导体", "changePercent": -2.2}],
}

CALENDAR_EVENTS: list[dict[str, object]] = [
    {
        "event_date": DATE_ISO,
        "event_time": "2026-11-27T07:30:00+08:00",
        "published_at": "2026-11-27T07:45:00+08:00",
        "title": "盘前政策口径",
        "content": "稳增长政策延续，未出现超预期收紧",
        "importance": "high",
        "source": "calendar:cn",
    },
    {
        "event_date": DATE_ISO,
        "event_time": "2026-11-27T10:10:00+08:00",
        "published_at": "2026-11-27T10:20:00+08:00",
        "title": "上午风格分化",
        "content": "成长板块继续领跌、防御板块抗跌",
        "importance": "high",
        "source": "calendar:cn",
    },
]


class _ReviewResult:
    def __init__(self, status: str, report: dict[str, object] | None) -> None:
        self.status = status
        self.report = report


class FakeNodeClient:
    """最小 NodeDataSource：返回真实形状的行情载荷。"""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_all:
            raise TimeoutError(f"{name} timeout")

    async def get_close_snapshot(self, snapshot_date: str) -> dict[str, object] | None:
        self._record(f"get_close_snapshot:{snapshot_date}")
        # 快照必须回填真实交易日：早报取的是上一交易日收盘，如果所有日期都返回
        # 同一个 trade_date，会被时间纪律整段过滤（这正是线上踩到的坑）。
        return {**CLOSE_SNAPSHOT, "trade_date": snapshot_date}

    async def get_last_close_snapshot(self) -> dict[str, object] | None:
        self._record("get_last_close_snapshot")
        return dict(LAST_CLOSE_SNAPSHOT)

    async def get_intraday_sectors(
        self, *, include_breadth: bool = True
    ) -> dict[str, object] | None:
        self._record("get_intraday_sectors")
        return dict(INTRADAY_SECTORS)

    async def get_calendar_events(
        self,
        date_from: str,
        date_to: str,
        *,
        importance: str | None = None,
    ) -> list[dict[str, object]] | None:
        self._record("get_calendar_events")
        return [dict(event) for event in CALENDAR_EVENTS]

    async def get_ths_index_map(self) -> list[dict[str, object]] | None:
        self._record("get_ths_index_map")
        return [{"name": "半导体", "ts_code": "885001"}]

    async def get_index_kline(
        self,
        code: str,
        days: int = 130,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, object]] | None:
        self._record("get_index_kline")
        return [{"trade_date": DATE_COMPACT, "close": 3012.4, "pct_chg": -1.18}]

    async def get_review_analysis_report(self, report_date: date) -> _ReviewResult:
        self._record("get_review_analysis_report")
        return _ReviewResult("not_found", None)


class _StructuredAnalystModel(BaseChatModel):
    """确定性假模型：产出 AnalystResult 工具调用，并回报真实 token 用量。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "p13-briefing-structured-fake"

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
                                    "claim": f"分析师结构化结论 #{self.calls}",
                                    "evidence_refs": [],
                                    "confidence": 0.62,
                                    "uncertainties": [],
                                    "requested_gaps": [],
                                },
                                "id": f"call_p13_{self.calls}",
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


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    reset_live_runs()
    yield
    reset_live_runs()


@pytest.fixture(autouse=True)
def _offline_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止测试触碰真实 thinking 模型：行情溯源子 run 也必须全离线。"""
    monkeypatch.setattr(
        "aistock_agent.brain_v4.runtime.graph_run._build_thinking_model_safe",
        lambda: None,
    )


async def test_morning_live_uses_analyst_and_records_real_tokens() -> None:
    morning = await run_live(
        "briefing_morning",
        TRADING_DATE,
        client=FakeNodeClient(),
        model=_StructuredAnalystModel(),
    )

    assert morning.judgment is not None
    assert morning.audit["token_source"] != "unavailable"
    assert int(morning.usage["total_tokens"]) > 0
    assert int(morning.audit["llm_claim_count"]) >= 1


async def test_midday_live_parent_and_incremental_changes() -> None:
    client = FakeNodeClient()
    morning = await run_live(
        "briefing_morning", TRADING_DATE, client=client, model=_StructuredAnalystModel()
    )
    midday = await run_live(
        "briefing_midday", TRADING_DATE, client=client, model=_StructuredAnalystModel()
    )

    assert morning.judgment is not None
    assert morning.judgment.judgment_id in midday.parent_judgment_ids
    # 冻结证据不取数：午报也必须拿到分析师 claim，而不是被工具证据拒绝。
    assert int(midday.audit["llm_claim_count"]) >= 1
    assert "JudgmentEvidenceError" not in str(midday.audit.get("degraded_reason") or "")

    revisions = midday.payload["revisions"]
    assert isinstance(revisions, list)
    assert revisions, "午报未产生任何增量变化"
    for change in revisions:
        assert isinstance(change, dict)
        assert change["parent_judgment_id"] == morning.judgment.judgment_id


async def test_briefing_session_skips_research_tools() -> None:
    """冻结证据不得再派发研究取数：图内 tool_calls 必须为 0。

    真实故障：午报研究任务取到 ledger 之外的 `ev_*_sector_intraday`，分析师引用后
    commit 被 JudgmentEvidenceError 拒绝，整份 LLM claim 丢失。
    """

    class _NoToolClient:
        async def fetch(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("briefing 不应调用取数工具")

    client = FakeNodeClient()
    inputs = await build_briefing_chain(client, TRADING_DATE, "briefing_morning")
    session = next(
        item for item in inputs.chain.sessions if item.profile == "briefing_morning"
    )

    outcome = await run_briefing_session(
        inputs.chain,
        session,
        model=_StructuredAnalystModel(),
        tool_client=_NoToolClient(),
    )

    assert outcome.judgment is not None
    assert int(outcome.usage["tool_calls"]) == 0
    assert outcome.analyst_claims, "冻结证据下分析师仍应产出 claim"
    assert not [e for e in outcome.errors if "JudgmentEvidenceError" in e]


async def test_evening_live_references_market_trace() -> None:
    client = FakeNodeClient()
    evening = await run_live(
        "briefing_evening", TRADING_DATE, client=client, model=_StructuredAnalystModel()
    )
    # 晚报内部已跑过市场溯源；同日同 profile 幂等，直接复用缓存。
    market_trace = await run_live(
        "market_trace", TRADING_DATE, client=client, model=_StructuredAnalystModel()
    )

    assert market_trace.judgment is not None
    assert evening.briefing_v1 is not None
    judgment_ids = evening.briefing_v1["judgment_ids"]
    assert isinstance(judgment_ids, list)
    assert market_trace.judgment.judgment_id in judgment_ids
    # 晚报的唯一 claim 槽位必须被分析师结论覆盖，不能再是 0 条。
    assert int(evening.audit["llm_claim_count"]) >= 1
    assert evening.payload["hypotheses"], "晚报盘后复核 claim 未进入 payload"
    assert evening.payload["hypotheses"][0]["claim_ref"] == "hyp_evening_setup"


async def test_model_absent_keeps_deterministic_and_marks_degraded() -> None:
    morning = await run_live(
        "briefing_morning", TRADING_DATE, client=FakeNodeClient(), model=None, use_llm=False
    )

    assert int(morning.usage["total_tokens"]) == 0
    assert morning.usage["prompt_tokens"] == 0
    assert morning.usage["completion_tokens"] == 0
    assert morning.audit["degraded_reason"]
    assert morning.audit["llm_claim_count"] == 0
    assert morning.judgment is not None
    # 确定性投影保持原样：claim 文本仍来自链式假设，而非 LLM。
    claims = morning.payload["hypotheses"]
    assert isinstance(claims, list)
    assert [claim["statement"] for claim in claims] == [
        "风格分化导致成长领跌",
        "流动性收缩是今日下跌主因",
        "板块轮动是次级因素",
    ]


async def test_graph_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # 分析师内核会把「单次模型调用异常」收敛成结构化错误结果，仍能给出研判；
    # 只有图级失败（此处模拟模型/图构建抛异常）才会走到回退路径。
    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("model exploded")

    monkeypatch.setattr(
        "aistock_agent.brain_v4.runtime.briefing_live.build_research_graph", _boom
    )

    morning = await run_live(
        "briefing_morning",
        TRADING_DATE,
        client=FakeNodeClient(),
        model=_StructuredAnalystModel(),
    )

    assert morning.judgment is not None
    assert morning.status != "ok"
    assert morning.audit["degraded_reason"]
    assert str(morning.audit["degraded_reason"]).startswith("briefing_graph_failed")
    assert morning.briefing_v1 is not None
    # 图失败仍保留确定性投影的 claim。
    claims = morning.payload["hypotheses"]
    assert isinstance(claims, list)
    assert claims
