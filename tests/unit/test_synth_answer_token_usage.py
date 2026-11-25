"""synth_answer 包装函数单测（P10 线 2）。

核心断言：synth_answer_node 在任意 return 路径统一写入 token_usage 键
（值来自 get_token_usage()，全 0/未采集为 None）。patch 目标必须是
synth_answer 模块命名空间内的名字（顶部 import 的 get_token_usage）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from aistock_agent.graph.nodes.synth_answer import synth_answer_node
from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.state.chat_schema import QuestionState


def _clarification_state() -> QuestionState:
    """走澄清短路路径（不触发 LLM），core 快速返回。

    注：goal 必须非 None——core 先查 goal is None 早退（返回"内部错误"），
    澄清分支在 goal 之后；与 test_synth_answer.py 的澄清用例同构。
    """
    return {
        "messages": [],
        "goal": InsightGoal(question="请提供股票代码", intent="stock_news"),
        "clarification": "请提供股票代码",
    }


@pytest.mark.asyncio
async def test_synth_answer_writes_token_usage_from_context() -> None:
    """包装函数把 get_token_usage() 快照写入 result["token_usage"]。"""
    fake_usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_token_usage",
        return_value=fake_usage,
    ):
        result = await synth_answer_node(_clarification_state())

    assert result["token_usage"] == fake_usage
    assert result["final_response"] == "请提供股票代码"  # core 行为不受影响


@pytest.mark.asyncio
async def test_synth_answer_token_usage_none_when_unset() -> None:
    """get_token_usage() 为 None（未采集/全 0）→ result["token_usage"] 为 None。"""
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_token_usage",
        return_value=None,
    ):
        result = await synth_answer_node(_clarification_state())

    assert result["token_usage"] is None
