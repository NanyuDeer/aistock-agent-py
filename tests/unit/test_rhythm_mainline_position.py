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


from aistock_agent.services.rhythm_engine import (
    EVENT_BRANCH_MAX_D, POSITION_LADDER, build_event_branch,
)


def test_position_ladder_is_absolute():
    # H1：绝对成数锚定
    assert POSITION_LADDER[0] == "空仓观望"
    assert "满仓" in POSITION_LADDER[4]
    assert len(POSITION_LADDER) == 5


def test_event_branch_none_when_d_beyond_max():
    ev = {"date": "2026-09-22", "title": "FOMC 议息", "importance": "high"}
    # origin=2026-09-16(周三)：交易日差到 09-22(周二) = {09-17,09-18,09-21,09-22}=4 > 3
    assert build_event_branch(ev, origin_date="2026-09-16") == []


def test_event_branch_kept_when_d_within_max():
    ev = {"date": "2026-09-18", "title": "FOMC 议息", "importance": "high"}
    out = build_event_branch(ev, origin_date="2026-09-16")
    assert len(out) == 3
    assert "不改变主档位" in out[0]["conclusion"]["note"]


def test_event_branch_without_origin_unchanged():
    ev = {"date": "2026-09-22", "title": "FOMC 议息", "importance": "high"}
    assert len(build_event_branch(ev)) == 3  # 无 origin 不启用 d 约束（向后兼容）


def test_event_branch_non_high_empty():
    assert build_event_branch({"date": "2026-09-15", "title": "X", "importance": "medium"}) == []
