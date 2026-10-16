"""工具失败降级（worker/工具层）单元测试。

原 e2e ``test_full_flow_tool_failure_degradation`` 断言「工具失败时回答里出现降级文案
而非报错」；但该 e2e 走旧 supervisor 路径（``/chat/message`` 已切 chat 子图）已不可达，
按人类决策下沉到本层单测：

- 工具层：底层依赖（node_api）抛异常 → ``safe_tool_call`` 返回 ``DEGRADED_MESSAGE``，
  不向上抛异常（真正的降级路径）；
- worker 层：模型/框架异常 → ``stock.run`` 返回兜底文案，不向上抛异常。

仅 mock 掉依赖（不发真实网络请求）；降级文案直接引用 ``DEGRADED_MESSAGE`` 常量
（或其中关键片段），不硬编码整段文本。
"""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.agents.workers.stock import run
from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.stock_tools import get_quote


@pytest.mark.asyncio
async def test_tool_returns_degraded_message_on_dependency_failure():
    """工具层降级路径：node_api.get 抛异常 → get_quote 返回降级文案而非异常。"""
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("upstream 500"))
        result = await get_quote.ainvoke({"symbol": "600519"})

    # 与常量同源（不复制粘贴整段文本）
    assert result == DEGRADED_MESSAGE
    assert "实时连接受限" in result
    assert "模拟分析" in result
    # 失败发生在工具内部：底层依赖确被调用到（证明走的是真实降级路径）
    mock_api.get.assert_awaited_once_with("/internal/quote/600519")


@pytest.mark.asyncio
async def test_stock_worker_degrades_instead_of_raising_on_llm_failure():
    """worker 层：LLM 工厂异常 → stock.run 返回兜底文案而非抛出异常。"""
    with patch(
        "aistock_agent.agents.workers.stock.get_deep_think",
        side_effect=RuntimeError("no api key"),
    ):
        result = await run(
            {"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]}
        )

    # worker 未向上抛异常，而是返回兜底（降级）文案
    assert "暂时不可用" in result["final_response"]
