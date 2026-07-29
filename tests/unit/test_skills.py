"""CHAT QA Skills 单元测试。"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.report_lookup import report_lookup


def _goal(intent: str = "report_lookup") -> InsightGoal:
    return InsightGoal(question="今天晨报说了什么", intent=intent)


@pytest.mark.asyncio
async def test_report_lookup_review_hit():
    """review 报告命中缓存 → 正常 Evidence。"""
    fake_artifact = {
        "schema_version": "1.1",
        "markdown": "# 复盘\n今日市场涨跌...",
        "trace_summary": "白酒板块领涨",
        "sectors": ["baijiu"],
    }
    with patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value=fake_artifact),
    ):
        ev = await report_lookup({"report_type": "review", "date": "2026-07-28"}, _goal())
    assert ev.skill_name == "report_lookup"
    assert ev.degraded is False
    assert len(ev.facts) >= 1
    assert any(s.kind == "db_report" for s in ev.sources)


@pytest.mark.asyncio
async def test_report_lookup_miss_returns_degraded():
    """缓存未命中 → degraded Evidence。"""
    with patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value=None),
    ):
        ev = await report_lookup({"report_type": "review", "date": "1999-01-01"}, _goal())
    assert ev.degraded is True
    assert "未找到" in (ev.degraded_reason or "") or "miss" in (ev.degraded_reason or "").lower()
