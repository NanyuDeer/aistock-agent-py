"""主线/仓位节奏相关确定性规则单测（spec §4.4/§5.2/§5.4）。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_calendar import is_high_importance_event


@pytest.mark.parametrize("title", [
    "美联储 9 月议息会议",
    "美国 8 月 CPI 数据公布",
    "9 月 FOMC 利率决议",
])
def test_macro_titles_promoted(title):
    assert is_high_importance_event(title, "L3") is True


@pytest.mark.parametrize("title", [
    "某公司回应关税影响，公司股价大跌",
    "英伟达发布新一代 GPU",
    "300750 宁王 8 月销量",
    "美联储官员内部讲话纪要（正文超过四十个字符的标题会被截断处理以保护布局）",
])
def test_company_or_long_titles_not_promoted(title):
    assert is_high_importance_event(title, "L3") is False


def test_elevated_source_l2_loose_match():
    assert is_high_importance_event("美 CPI 前瞻", "L2") is True
    assert is_high_importance_event("CPI", None) is True


def test_load_event_window_normalizes_importance():
    from aistock_agent.services import event_calendar as ec

    with patch.object(ec, "node_api") as api:
        api.get_calendar_events = AsyncMock(return_value=[
            {"date": "2026-09-15", "title": "美联储 9 月议息会议", "importance": "medium",
             "source": "L3", "type": "macro"},
            {"date": "2026-09-16", "title": "某公司回应关税影响", "importance": "medium",
             "source": "L3", "type": "macro"},
        ])
        win = asyncio.run(ec.load_event_window("2026-09-15"))
    assert win.events[0]["importance"] == "high"
    assert win.events[1]["importance"] == "medium"
    assert len(win.high_events) == 1
