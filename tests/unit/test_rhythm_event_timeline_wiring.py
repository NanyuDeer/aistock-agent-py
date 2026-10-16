"""事件时间线 → 节奏大师卡片接线（Task 3 / I1）。

防回归背景：本文件此前为 vacuous 测试——patch `rm.load_event_timeline` 后又直接调用
`rm.load_event_timeline`，从未经过 `_compose_card`；即使 `_compose_card` 退回旧的
日历加载器 `load_event_window` 也恒 pass。现改为真实驱动 `_compose_card`：

- 用替身注入「时间线哨兵事件」并记录调用参数（必须经 `load_event_timeline`，且请求
  全量展示窗 `horizon_days=None`）；
- 用哨兵注入日历加载通道 `node_api.get_calendar_events` 并做否定断言（一次都不得被触发，
  若接线退回 `load_event_window` 立即变红）。

断言口径：时间线事件必须流入卡片输出（`win.events` / `event_window` /
`event_high_hint` / `next_event_anchor`）。
"""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aistock_agent.agents.workers import rhythm_master as rm
from aistock_agent.services.event_calendar import EventWindow

_TIMELINE_TITLE = "时间线哨兵事件（CPI）"
_CALENDAR_TITLE = "日历哨兵事件（不得出现）"


def _kline_rows(n: int = 200) -> list[dict]:
    """上行 K 线（末日 = 运行日，避开 after_close basis 门禁）。"""
    rows = []
    for i in range(n):
        c = 3000.0 + i
        rows.append(
            {
                "trade_date": "20260910",
                "open": c - 1,
                "high": c + 2,
                "low": c - 2,
                "close": c,
                "pct_chg": 0.1,
                "vol": 100,
                "amount": 120.0,
            }
        )
    return rows


@pytest.fixture
def isolated_sentiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 sentiment 归档，避免读真实仓库文件（复用 worker 集成测试同款做法）。"""
    monkeypatch.setattr(rm, "sentiment_archive_dir", tmp_path)


@pytest.mark.asyncio
async def test_compose_card_routes_timeline_events_into_card_output(
    isolated_sentiment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int | None]] = []

    async def fake_timeline(
        target_date: str, horizon_days: int | None = None
    ) -> EventWindow:
        calls.append((target_date, horizon_days))
        # 事件日 == target_date（d=0），确保落入分析子窗并被锚点 / hint 采用。
        sentinel = {
            "date": target_date,
            "type": "macro",
            "title": _TIMELINE_TITLE,
            "importance": "high",
        }
        return EventWindow(
            events=[sentinel], high_events=[sentinel], display_events=[sentinel]
        )

    calendar_spy = AsyncMock(
        return_value=[
            {"date": "2026-09-11", "title": _CALENDAR_TITLE, "importance": "high"}
        ]
    )
    monkeypatch.setattr(rm, "load_event_timeline", fake_timeline)
    # 必须 patch 类方法：实例属性还原会在 node_api 单例上留下遮蔽类属性的实例属性（污染回放隔离）
    monkeypatch.setattr(type(rm.node_api), "get_calendar_events", calendar_spy)
    monkeypatch.setattr(
        type(rm.node_api), "get_index_kline", AsyncMock(return_value=_kline_rows())
    )
    monkeypatch.setattr(
        type(rm.node_api), "get_fear_greed", AsyncMock(return_value={"index": 40})
    )
    monkeypatch.setattr(
        type(rm.node_api),
        "get_close_snapshot",
        AsyncMock(return_value={"breadth": {"total_count": 100, "advance_count": 60}}),
    )
    monkeypatch.setattr(
        type(rm.node_api), "get_rhythm_report", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(rm, "load_mainline_candidates", lambda: (False, []))
    monkeypatch.setattr(rm, "run_synthesis", AsyncMock(return_value=None))
    monkeypatch.setattr(rm, "validate_synthesis", lambda *args, **kwargs: False)

    card, rows, win = await rm._compose_card("2026-09-10", "after_close")
    rhythm_card = rm._build_rhythm_card(card, win, rows, card.mainline_facts)

    # 接线 1：必须经时间线加载器取数，且请求全量展示窗（horizon_days=None，需求 2）。
    assert len(calls) == 1
    target_date, horizon_days = calls[0]
    assert horizon_days is None
    assert target_date == "2026-09-11"  # after_close → 运行日次一交易日
    # 接线 2：日历加载通道一次都不得被触发（退回 load_event_window 即变红）。
    calendar_spy.assert_not_awaited()
    # 接线 3：时间线事件流入分析子窗（供 confirm / 锚点 / 分支）。
    assert [e["title"] for e in win.events] == [_TIMELINE_TITLE]
    assert [e["title"] for e in win.high_events] == [_TIMELINE_TITLE]
    # 接线 4：时间线事件流入卡片输出（展示窗 + 临近提示 + 锚点），日历哨兵不得出现。
    assert [e["title"] for e in rhythm_card["event_window"]] == [_TIMELINE_TITLE]
    assert _TIMELINE_TITLE in rhythm_card["event_high_hint"]
    assert rhythm_card["next_event_anchor"]["title"] == _TIMELINE_TITLE
    assert _CALENDAR_TITLE not in str(rhythm_card)
