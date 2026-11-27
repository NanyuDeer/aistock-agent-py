"""P3 实时运行器集成测试：真实取数入口 + 主图 + 投影 + 幂等/父子/降级。

全部离线：`FakeNodeClient` 只实现本包用到的只读方法并返回固定数据，
`FakeModel` 是确定性假聊天模型（运行器只记录注入、不真正推理），
不触网、不读系统时间、不调用真实 LLM。

覆盖（对应 P3 执行包验收点）：
- market_trace 产出 judgment + 六因子，snapshot_id/sha 确定且同输入同 SHA；
- 早/午/晚 parent 链：morning 无 parent，midday 指向 morning，evening 指向
  morning + midday 且引用 market_trace judgment；
- 同日同 profile 幂等：重复调用返回已有 run 且不再取数；
- 取数失败/超时降级为 partial/insufficient，不抛裸异常；
- 时间边界：published_at 晚于早报 cutoff 的证据不得进入早报 run。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.features.market_trace.projection import (
    SIX_FACTOR_STAGES,
)
from aistock_agent.brain_v4.runtime import (
    LiveRunResult,
    reset_live_runs,
    run_live,
)

CST = timezone(timedelta(hours=8))
TRADING_DATE = date(2026, 11, 27)
DATE_ISO = "2026-11-27"
DATE_COMPACT = "20261127"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


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
        # event_time 在早报 cutoff 之前，但 published_at 晚于早报 cutoff：
        # 这是“盘中才发布”的时间边界样本，只能进午报。
        "event_date": DATE_ISO,
        "event_time": "2026-11-27T07:00:00+08:00",
        "published_at": "2026-11-27T10:00:00+08:00",
        "title": "盘前事件盘中才发布",
        "content": "隔夜政策细节盘中补充披露",
        "importance": "medium",
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
    {
        # event_time 晚于 market_trace cutoff（15:00）：收盘后补录，必须被剔除。
        "event_date": DATE_ISO,
        "event_time": "2026-11-27T15:10:00+08:00",
        "published_at": "2026-11-27T15:15:00+08:00",
        "title": "收盘后补录事件",
        "content": "收盘后才补录的事件",
        "importance": "low",
        "source": "calendar:cn",
    },
]

LEGACY_REPORT: dict[str, object] = {
    "headline": "旧链结论：流动性收缩是今日下跌主因",
    "root_cause": "liquidity_contraction",
    "evidence_ids": ["legacy_review_ev_1"],
}


class _ReviewResult:
    """结构化复盘读取结果；只暴露输入构建需要的两个字段。"""

    def __init__(self, status: str, report: dict[str, object] | None) -> None:
        self.status = status
        self.report = report


class FakeNodeClient:
    """内存假 NodeApiClient：记录调用次数并按固定数据返回。"""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_all:
            raise TimeoutError(f"{name} timeout")

    async def get_close_snapshot(self, snapshot_date: str) -> dict[str, object] | None:
        self._record("get_close_snapshot")
        return dict(CLOSE_SNAPSHOT)

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
        return [
            {"trade_date": DATE_COMPACT, "close": 3012.4, "pct_chg": -1.18}
        ]

    async def get_review_analysis_report(self, report_date: date) -> _ReviewResult:
        self._record("get_review_analysis_report")
        return _ReviewResult("found", dict(LEGACY_REPORT))


class FakeModel(BaseChatModel):
    """确定性假模型：运行器只记录“已注入”，不真正调用模型。"""

    @property
    def _llm_type(self) -> str:
        return "runtime-fake-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> FakeModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=messages[-1])])


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    reset_live_runs()
    yield
    reset_live_runs()


def _titles(result: LiveRunResult) -> list[str]:
    """从 payload 的证据投影里取标题，用于时间边界断言。

    市场溯源证据的 tool_params 不复制标题，因此补一张 source_id -> title 表；
    两个 profile 共用同一断言口径。
    """
    title_by_source: dict[str, str] = {}
    sources = result.payload.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_id = source.get("source_id")
            title = source.get("title")
            if isinstance(source_id, str) and isinstance(title, str):
                title_by_source[source_id] = title

    evidence = result.payload.get("evidence")
    assert isinstance(evidence, list)
    titles: list[str] = []
    for item in evidence:
        assert isinstance(item, dict)
        params = item.get("tool_params")
        if not isinstance(params, dict):
            continue
        title = params.get("title")
        if isinstance(title, str):
            titles.append(title)
            continue
        source_id = params.get("source_id")
        if isinstance(source_id, str) and source_id in title_by_source:
            titles.append(title_by_source[source_id])
    return titles


@pytest.mark.asyncio
async def test_market_trace_run_produces_six_factor_and_deterministic_sha() -> None:
    client = FakeNodeClient()
    result = await run_live(
        "market_trace", TRADING_DATE, client=client, model=FakeModel()
    )

    assert result.profile == "market_trace"
    assert result.judgment is not None
    assert result.six_factor is not None
    assert [step.stage for step in result.six_factor.steps] == list(
        SIX_FACTOR_STAGES
    )
    assert result.snapshot_id
    assert len(result.snapshot_sha) == 64
    assert result.as_of == _dt("2026-11-27T15:35:00+08:00")
    assert result.data_cutoff == _dt("2026-11-27T15:00:00+08:00")

    # 同日同 profile 幂等：返回已有 run，且不再取数。
    before = len(client.calls)
    again = await run_live(
        "market_trace", TRADING_DATE, client=client, model=FakeModel()
    )
    assert again is result
    assert len(client.calls) == before

    # 清空注册表后用同输入重跑：snapshot_id / SHA 必须完全一致。
    reset_live_runs()
    fresh = await run_live(
        "market_trace", TRADING_DATE, client=FakeNodeClient(), model=FakeModel()
    )
    assert fresh.snapshot_id == result.snapshot_id
    assert fresh.snapshot_sha == result.snapshot_sha


@pytest.mark.asyncio
async def test_briefing_parent_chain_and_market_trace_reference() -> None:
    client = FakeNodeClient()
    morning = await run_live(
        "briefing_morning", TRADING_DATE, client=client, model=FakeModel()
    )
    assert morning.parent_run_id is None
    assert morning.parent_judgment_ids == []
    assert morning.judgment is not None
    assert morning.briefing_v1 is not None
    assert morning.briefing_v1["slot"] == "pre"

    midday = await run_live(
        "briefing_midday", TRADING_DATE, client=client, model=FakeModel()
    )
    assert midday.parent_run_id == morning.run_id
    assert midday.parent_judgment_ids == [morning.judgment.judgment_id]
    assert midday.briefing_v1 is not None
    assert midday.briefing_v1["slot"] == "mid"

    market_trace = await run_live(
        "market_trace", TRADING_DATE, client=client, model=FakeModel()
    )
    evening = await run_live(
        "briefing_evening", TRADING_DATE, client=client, model=FakeModel()
    )
    assert market_trace.judgment is not None
    assert evening.parent_run_id == midday.run_id
    assert evening.parent_judgment_ids == [
        morning.judgment.judgment_id,
        midday.judgment.judgment_id,
    ]
    assert evening.briefing_v1 is not None
    assert evening.briefing_v1["slot"] == "post"
    judgment_ids = evening.briefing_v1["judgment_ids"]
    assert isinstance(judgment_ids, list)
    assert market_trace.judgment.judgment_id in judgment_ids
    extras = evening.briefing_v1["evening_extras"]
    assert isinstance(extras, dict)
    assert extras["breadth"] is not None


@pytest.mark.asyncio
async def test_same_day_same_profile_is_idempotent_without_refetch() -> None:
    client = FakeNodeClient()
    first = await run_live(
        "briefing_morning", TRADING_DATE, client=client, model=FakeModel()
    )
    calls_after_first = len(client.calls)
    assert calls_after_first > 0

    second = await run_live(
        "briefing_morning", TRADING_DATE, client=client, model=FakeModel()
    )
    assert second is first
    assert len(client.calls) == calls_after_first


@pytest.mark.asyncio
async def test_fetch_failure_degrades_to_partial_or_insufficient() -> None:
    client = FakeNodeClient(fail_all=True)

    market_trace = await run_live(
        "market_trace", TRADING_DATE, client=client, model=FakeModel()
    )
    assert market_trace.status in {"partial", "insufficient"}

    morning = await run_live(
        "briefing_morning", TRADING_DATE, client=client, model=FakeModel()
    )
    assert morning.status in {"partial", "insufficient"}
    # 降级也必须给出符合前端契约的 briefing.v1，而不是抛裸异常。
    assert morning.briefing_v1 is not None
    assert morning.briefing_v1["schema_version"] == "briefing.v1"
    assert morning.briefing_v1["audio_path"] is None


@pytest.mark.asyncio
async def test_evidence_published_after_cutoff_is_excluded() -> None:
    client = FakeNodeClient()
    morning = await run_live(
        "briefing_morning", TRADING_DATE, client=client, model=FakeModel()
    )
    midday = await run_live(
        "briefing_midday", TRADING_DATE, client=client, model=FakeModel()
    )
    market_trace = await run_live(
        "market_trace", TRADING_DATE, client=client, model=FakeModel()
    )

    morning_titles = _titles(morning)
    midday_titles = _titles(midday)
    market_trace_titles = _titles(market_trace)

    # 盘前发生、但盘中才发布：早报不可见，午报可见。
    assert "盘前事件盘中才发布" not in morning_titles
    assert "盘前事件盘中才发布" in midday_titles
    assert "盘前政策口径" in morning_titles

    # 晚于 market_trace cutoff 的补录事件必须被剔除。
    assert "收盘后补录事件" not in market_trace_titles
    assert "上午风格分化" in market_trace_titles
