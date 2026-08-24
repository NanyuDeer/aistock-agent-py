"""盘中报 worker 核心逻辑测试（MVP：quick_think + 晨报上下文 + midday 落库）。"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import midday as midday_agent
from aistock_agent.state.schema import AgentState


@pytest.fixture
def base_state() -> AgentState:
    return {
        "messages": [],
        "session_id": "test_midday",
        "user_id": None,
        "favorites": [],
        "intent": "midday",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "report_date": "2026-08-24",
        "final_response": None,
    }


def _fake_report_dict() -> dict[str, object]:
    return {
        "display_report": {
            "summary": "上午指数分化，午后关注量能",
            "details": "沪深主要指数上午涨跌互现，" + ("数据" * 60),
            "stocks": [],
            "risks": ["量能不足"],
        },
        "podcast_brief": "上午盘面回顾示意。",
        "schema_version": "2.0",
    }


@pytest.mark.asyncio
async def test_run_persists_midday(base_state):
    # 缓存空 → 库读晨报上下文失败 → mock 掉（规范8：测试不依赖真实外部服务）
    agent_run_mock = AsyncMock(return_value=_fake_report_dict())
    persist_mock = AsyncMock(return_value=True)
    with (
        patch.object(midday_agent, "_invoke_agent", agent_run_mock),
        patch.object(midday_agent, "persist_midday_report", persist_mock),
        patch.object(
            midday_agent,
            "_resolve_morning_context",
            AsyncMock(return_value="今日晨报结论示例"),
        ),
    ):
        result = await midday_agent.run(base_state)
    assert "final_response" in result
    parsed = json.loads(result["final_response"])
    assert parsed["schema_version"] == "2.0"
    assert parsed["display_report"]["stocks"] == []
    assert result["analysis_reports"]["midday_generated"] is True
    assert result["analysis_reports"]["midday_persisted"] is True
    persist_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_degraded_does_not_persist(base_state):
    degraded = {
        "display_report": {"summary": "", "details": "x", "stocks": [], "risks": []},
        "podcast_brief": "",
        "schema_version": "1.0",
    }
    with (
        patch.object(midday_agent, "_invoke_agent", AsyncMock(return_value=degraded)),
        patch.object(midday_agent, "persist_midday_report", AsyncMock(return_value=False)),
        patch.object(
            midday_agent,
            "_resolve_morning_context",
            AsyncMock(return_value="今日晨报结论示例"),
        ),
    ):
        result = await midday_agent.run(base_state)
    assert result["analysis_reports"]["midday_persisted"] is False