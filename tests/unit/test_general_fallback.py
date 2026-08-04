"""general_fallback 节点测试（P7+P8 线 1 Task 3）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.graph.nodes.general_fallback import general_fallback_node
from aistock_agent.state.chat_schema import QuestionState


def _state(source: str) -> QuestionState:
    from langchain_core.messages import HumanMessage

    return {"general_source": source, "messages": [HumanMessage(content="测试问题")]}  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_science_mode_calls_run_science() -> None:
    with patch(
        "aistock_agent.graph.nodes.general_fallback.run_science",
        AsyncMock(return_value="市盈率是股价与每股收益的比值"),
    ) as science_mock, patch(
        "aistock_agent.graph.nodes.general_fallback.run_gap"
    ) as gap_mock:
        out = await general_fallback_node(_state("science"))
    science_mock.assert_awaited_once()
    gap_mock.assert_not_awaited()
    assert out["final_response"] == "市盈率是股价与每股收益的比值"


@pytest.mark.asyncio
async def test_gap_mode_calls_run_gap_and_marks_skill_request() -> None:
    with patch(
        "aistock_agent.graph.nodes.general_fallback.run_gap",
        AsyncMock(return_value="美联储加息对A股的影响需结合汇率分析"),
    ) as gap_mock, patch(
        "aistock_agent.graph.nodes.general_fallback._log_skill_request"
    ) as log_mock:
        out = await general_fallback_node(_state("gap"))
    gap_mock.assert_awaited_once()
    log_mock.assert_awaited_once()
    assert "加息" in out["final_response"]


@pytest.mark.asyncio
async def test_missing_source_falls_back_to_gap_semantics() -> None:
    # general_source 缺失（防御）：按缺口模式处理，保证 final_response 恒非空
    with patch(
        "aistock_agent.graph.nodes.general_fallback.run_gap",
        AsyncMock(return_value="兜底回答"),
    ), patch(
        "aistock_agent.graph.nodes.general_fallback._log_skill_request"
    ):
        out = await general_fallback_node(_state("gap"))
    assert out["final_response"] == "兜底回答"
