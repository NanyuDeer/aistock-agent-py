"""预期差判定（spec §5.11/裁决 C3 + 硬约束 X2）：谓词 [昨日,今日] 已公布、日内重试。"""
from datetime import date

import pytest

from aistock_agent.services.forward_event_llm import _extract_consensus, judge_expectation_diff
from aistock_agent.services.forward_events import run_expectation_diff


def test_extract_consensus_from_detail():
    assert _extract_consensus("国家统计局年度发布日程｜consensus:同比 +0.6%") == "同比 +0.6%"
    assert _extract_consensus("无共识标注") is None


def test_judge_expectation_diff_maps_result():
    # LLM 返回 超预期 → 归一化到枚举
    assert judge_expectation_diff("title", "同比 +0.6%", "同比 +0.9%", verdict="超预期") == "超预期"
    assert judge_expectation_diff(
        "title", "同比 +0.6%", "同比 +0.5%", verdict="不及预期") == "不及预期"
    assert judge_expectation_diff(
        "title", "同比 +0.6%", "同比 +0.6%", verdict="符合预期") == "符合"


@pytest.mark.asyncio
async def test_run_expectation_diff_only_yesterday_today(monkeypatch):
    """谓词：只判 event_date ∈ [昨日,今日] 已公布；2 日前事件不判。

    读侧契约（M1/C1）：行键为 `date`（非 event_date），detail 透传 consensus。
    """
    fetched: list[tuple[str, str, str | None]] = []

    async def fake_get(d_from, d_to, importance=None):
        fetched.append((d_from, d_to, importance))
        return [
            {"date": "2026-09-21", "title": "昨日事件", "importance": "high",
             "result": None, "detail": "x｜consensus:1%"},
            {"date": "2026-09-18", "title": "两天前事件", "importance": "high",
             "result": None, "detail": "x｜consensus:1%"},
        ]

    async def fake_post(body):
        return {"code": 0, "data": {"id": 1, "upserted": False}}

    async def fake_search(title):
        return {"outcome": "ok", "results": [{"title": title, "content": "公布值 2%"}]}

    async def fake_judge(title, consensus, actual):
        return "超预期"

    m = monkeypatch
    m.setattr(
        "aistock_agent.services.forward_events.node_api.get_calendar_events", fake_get)
    m.setattr(
        "aistock_agent.services.forward_events.node_api.post_calendar_event", fake_post)
    m.setattr(
        "aistock_agent.services.forward_events.shanghai_today",
        lambda: date(2026, 9, 22))
    m.setattr(
        "aistock_agent.services.forward_events.prev_trading_day",
        lambda d: date(2026, 9, 21))
    m.setattr(
        "aistock_agent.services.forward_events._search_actual_value", fake_search)
    m.setattr("aistock_agent.services.forward_events._llm_judge", fake_judge)

    result = await run_expectation_diff("2026-09-22")
    assert result["judged"] == 1  # 仅昨日事件被判（两天前被谓词过滤）
    assert result["skipped_past_window"] == 1


@pytest.mark.asyncio
async def test_run_expectation_diff_consensus_unreachable_skips(monkeypatch):
    """锁 C1：事件有 date 但 detail 缺失（consensus 不可达）→ skipped_consensus。

    detail 必须透传才判（app-api toContractEvent 加性透传 detail 后可达；
    若透传被移除，本判例会因 detail 缺失而 skipped_consensus → 可证伪 C1 修复）。
    """
    async def fake_get(d_from, d_to, importance=None):
        return [
            {"date": "2026-09-21", "title": "有日期无 detail 事件", "importance": "high",
             "result": None, "detail": None},
        ]

    async def fake_post(body):
        return {"code": 0, "data": {"id": 1, "upserted": False}}

    m = monkeypatch
    m.setattr(
        "aistock_agent.services.forward_events.node_api.get_calendar_events", fake_get)
    m.setattr(
        "aistock_agent.services.forward_events.node_api.post_calendar_event", fake_post)
    m.setattr(
        "aistock_agent.services.forward_events.shanghai_today",
        lambda: date(2026, 9, 22))
    m.setattr(
        "aistock_agent.services.forward_events.prev_trading_day",
        lambda d: date(2026, 9, 21))

    result = await run_expectation_diff("2026-09-22")
    assert result["skipped_consensus"] == 1
    assert result["judged"] == 0
    assert result["attempted"] == 0
