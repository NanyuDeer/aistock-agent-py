"""hot_burst_agent 测试"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.agents.workers.hot_burst import run

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.hot_burst.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.hot_burst.get_deep_think"


@pytest.mark.asyncio
async def test_hot_burst_agent_returns_ai_message():
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="这是机构调研热门股解读")]}
    )

    with patch(_CREATE_REACT_AGENT, return_value=mock_agent), patch(
        _GET_DEEP_THINK,
        return_value=MagicMock(),
    ):
        result = await run(
            {
                "messages": [{"role": "user", "content": "分析今天的机构调研热门股"}],
                "session_id": "s1",
                "user_id": None,
                "favorites": [],
                "intent": "hot_burst",
                "symbol": None,
                "tag_code": None,
                "analysis_reports": {},
                "final_response": None,
            }
        )

    assert result["final_response"] == "这是机构调研热门股解读"
    assert result["analysis_reports"]["hot_burst"] == "这是机构调研热门股解读"
