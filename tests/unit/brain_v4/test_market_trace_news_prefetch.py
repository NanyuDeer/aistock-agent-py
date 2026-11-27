"""P26：市场溯源快照阶段预取新闻并冻结（全部离线）。

目标行为（north-star）：资深分析师开工前先读新闻底稿。本文件验证：
- 当日电报被归一化成 `NEWS_xxx` 事件证据，可被研判引用；
- `published_at > data_cutoff` 的新闻一律剔除并计入 time_leakage；
- 按重要性取 TopN，标题不被摘要截断吞掉；
- 新闻不可用时如实记 missing，绝不编造。

不触网、不读系统时间。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aistock_agent.brain_v4.analysts.context import build_analyst_task
from aistock_agent.brain_v4.features.market_trace.adapter import snapshot_to_evidence
from aistock_agent.brain_v4.runtime.inputs import NEWS_TOPN, build_market_trace_case

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
DATA_CUTOFF = datetime(2026, 11, 27, 15, 0, tzinfo=CST)

CLOSE_SNAPSHOT: dict[str, object] = {
    "status": "complete",
    "trade_date": "20261127",
    "captured_at": "2026-11-27T15:10:00+08:00",
    "indexes": [
        {
            "code": "000001.SH",
            "name": "上证指数",
            "close": 3020.5,
            "change_pct": -0.82,
        }
    ],
    "breadth": {
        "total_count": 5200,
        "advance_count": 1900,
        "decline_count": 3150,
        "flat_count": 150,
    },
    "turnover": {"amount_yuan": 760000000000, "change_pct": -0.11},
    "sectors": {"defensive": {"change_pct": 0.4}, "growth": {"change_pct": -1.3}},
}


class _ReviewResult:
    def __init__(self, status: str, report: dict[str, object] | None = None) -> None:
        self.status = status
        self.report = report


def _news_item(
    news_id: int, title: str, *, at: datetime, content: str = ""
) -> dict[str, object]:
    return {
        "id": str(news_id),
        "title": title,
        "content": content or f"{title}的正文",
        "time": at.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(at.timestamp()),
    }


class _NewsClient:
    def __init__(
        self,
        *,
        news: dict[str, object] | None,
        close: dict[str, object] | None = CLOSE_SNAPSHOT,
    ) -> None:
        self._news = news
        self._close = close
        self.calls: list[str] = []

    async def get_close_snapshot(
        self, snapshot_date: str
    ) -> dict[str, object] | None:
        self.calls.append("get_close_snapshot")
        return None if self._close is None else dict(self._close)

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
        self.calls.append("get_news_telegraph")
        return self._news

    async def get_review_analysis_report(self, report_date: date) -> _ReviewResult:
        return _ReviewResult("not_found")


def _payload(items: list[dict[str, object]]) -> dict[str, object]:
    return {"date": "2026-11-27", "items": items, "total": len(items), "degraded": False}


@pytest.mark.asyncio
async def test_news_prefetched_with_future_filtered_and_title_kept() -> None:
    morning = datetime(2026, 11, 27, 9, 12, tzinfo=CST)
    noon = datetime(2026, 11, 27, 12, 5, tzinfo=CST)
    after_close = datetime(2026, 11, 27, 15, 20, tzinfo=CST)
    client = _NewsClient(
        news=_payload(
            [
                _news_item(
                    1,
                    "国常会：加力发挥国家队稳市作用，优化中央汇金类平准基金功能",
                    at=morning,
                ),
                _news_item(2, "某公司发布三季度业绩预告", at=noon),
                _news_item(3, "盘后：海外市场突发波动", at=after_close),
            ]
        )
    )

    inputs = await build_market_trace_case(client, AS_OF, DATA_CUTOFF, "snap_p26")

    news_sources = {
        source_id: source
        for source_id, source in inputs.case.snapshot.sources.items()
        if source_id.startswith("NEWS_")
    }
    assert len(news_sources) == 2
    titles = [source.title for source in news_sources.values()]
    assert any("国常会" in title for title in titles)
    assert all("盘后" not in title for title in titles)
    # 未来新闻不得入账，但必须计入时间泄漏审计。
    assert inputs.time_leakage_count == 1
    assert "news_telegraph" not in inputs.missing_fields
    assert inputs.case.snapshot.collection_status["news"].state == "available"
    # 标题必须保留在正文开头（分析师任务文本按 300 字截断时仍能看到标题）。
    assertive = next(
        source for source in news_sources.values() if "国常会" in source.title
    )
    assert assertive.content.startswith(assertive.title)
    assert assertive.origin == "domestic"


@pytest.mark.asyncio
async def test_news_topn_keeps_most_important_items() -> None:
    base = datetime(2026, 11, 27, 9, 0, tzinfo=CST)
    items = [
        _news_item(100 + index, f"行业常规快讯 {index}", at=base)
        for index in range(NEWS_TOPN + 5)
    ]
    items.append(_news_item(999, "央行宣布降准 0.5 个百分点", at=base))
    client = _NewsClient(news=_payload(items))

    inputs = await build_market_trace_case(client, AS_OF, DATA_CUTOFF, "snap_p26_topn")

    news_sources = [
        source
        for source_id, source in inputs.case.snapshot.sources.items()
        if source_id.startswith("NEWS_")
    ]
    assert len(news_sources) == NEWS_TOPN
    assert any("降准" in source.title for source in news_sources)


@pytest.mark.asyncio
async def test_missing_news_is_recorded_not_invented() -> None:
    client = _NewsClient(news=None)

    inputs = await build_market_trace_case(client, AS_OF, DATA_CUTOFF, "snap_p26_missing")

    assert "news_telegraph" in inputs.missing_fields
    assert inputs.case.snapshot.collection_status["news"].state == "unavailable"
    assert not any(
        source_id.startswith("NEWS_") for source_id in inputs.case.snapshot.sources
    )


@pytest.mark.asyncio
async def test_empty_news_payload_is_recorded_as_empty() -> None:
    client = _NewsClient(news=_payload([]))

    inputs = await build_market_trace_case(client, AS_OF, DATA_CUTOFF, "snap_p26_empty")

    assert "news_telegraph" in inputs.missing_fields
    assert inputs.case.snapshot.collection_status["news"].state == "empty"


@pytest.mark.asyncio
async def test_news_is_visible_in_analyst_task_text() -> None:
    """预取新闻必须真的进入分析师可见文本，否则冻结了也没人用。"""
    at = datetime(2026, 11, 27, 9, 12, tzinfo=CST)
    items = [
        _news_item(200 + index, f"行业常规快讯 {index}", at=at)
        for index in range(NEWS_TOPN)
    ]
    items.append(
        _news_item(
            999,
            "国常会：加力发挥国家队稳市作用，优化中央汇金类平准基金功能",
            at=at,
        )
    )
    client = _NewsClient(news=_payload(items))
    inputs = await build_market_trace_case(client, AS_OF, DATA_CUTOFF, "snap_p26_visible")

    snapshot = inputs.case.snapshot
    evidence = snapshot_to_evidence(snapshot, AS_OF, DATA_CUTOFF, snapshot.snapshot_id)
    task = build_analyst_task(
        mode="research",
        role="lead_thesis",
        round_no=0,
        subject="market",
        goal=None,
        hypotheses=[],
        evidence=evidence,
        sources={sid: record.content for sid, record in snapshot.sources.items()},
    )

    assert "国常会" in task
    assert "NEWS" in task.upper()

