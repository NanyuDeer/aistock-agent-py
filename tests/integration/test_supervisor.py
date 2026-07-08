"""supervisor.run 集成测试 — 意图分类端到端链路

聚焦 supervisor.run 的端到端行为：mock LLM（get_quick_think）→ ainvoke →
parse_intent → 返回 dict。不重复 utils.parser.parse_intent 的单元测试
（已在 tests/unit/test_utils_parser.py 覆盖 9 例）。

覆盖 brief 要求的边界用例：
- 5 类意图分类（morning/stock/sector/event/general）
- 空消息降级、无 human 消息降级（不调用 LLM）
- LLM 输出无法解析降级
- 多模态 content（list 形态）转 str 不崩溃
- 连续 7 位数字不提取为 symbol
- 字母夹数字不提取为 symbol
- BK 板块码大小写归一化
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.agents.supervisor.node import run

# patch 目标：supervisor.node 模块内导入的 get_quick_think 引用
_GET_QUICK_THINK = "aistock_agent.agents.supervisor.node.get_quick_think"


def _mock_llm(content: object) -> MagicMock:
    """构造 mock LLM：ainvoke 为 AsyncMock，返回 content 可控的 response。

    content 通常是 str；传入 list 可模拟多模态输出。
    """
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = content
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    return mock_llm


# ── 5 类意图分类 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_morning_intent():
    """LLM 输出 'morning' → intent='morning'"""
    state = {"messages": [HumanMessage(content="生成今日晨报")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("morning")):
        result = await run(state)
    assert result["intent"] == "morning"
    assert result["symbol"] is None
    assert result["tag_code"] is None


@pytest.mark.asyncio
async def test_supervisor_stock_intent_with_symbol():
    """LLM 输出 'stock' + 用户消息含 6 位代码 → intent='stock', symbol=代码"""
    state = {"messages": [HumanMessage(content="分析 600519 的走势")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("stock")):
        result = await run(state)
    assert result["intent"] == "stock"
    assert result["symbol"] == "600519"
    assert result["tag_code"] is None


@pytest.mark.asyncio
async def test_supervisor_sector_intent_with_tag_code():
    """LLM 输出 'sector' + 用户消息含 BK 代码 → intent='sector', tag_code 归一化大写"""
    state = {"messages": [HumanMessage(content="分析 bk0475 白酒板块")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("sector")):
        result = await run(state)
    assert result["intent"] == "sector"
    assert result["symbol"] is None
    assert result["tag_code"] == "BK0475"


@pytest.mark.asyncio
async def test_supervisor_event_intent():
    """LLM 输出 'event' → intent='event'"""
    state = {"messages": [HumanMessage(content="分析美联储加息对市场的影响")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("event")):
        result = await run(state)
    assert result["intent"] == "event"
    assert result["symbol"] is None
    assert result["tag_code"] is None


@pytest.mark.asyncio
async def test_supervisor_general_fallback():
    """LLM 输出无任何类别关键词 → intent 回退 'general'"""
    state = {"messages": [HumanMessage(content="你好")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("这是一个分析请求")):
        result = await run(state)
    assert result["intent"] == "general"
    assert result["symbol"] is None
    assert result["tag_code"] is None


# ── 边界用例 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_empty_messages_returns_general():
    """messages=[] → 不调用 LLM，直接降级 general"""
    state: dict = {"messages": []}
    mock_llm = _mock_llm("stock")
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await run(state)
    assert result == {"intent": "general"}
    # 空消息不应触发 LLM 调用
    mock_llm.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_supervisor_no_human_message_returns_general():
    """messages 只有 ai 消息 → 不调用 LLM，降级 general"""
    state = {"messages": [AIMessage(content="这是一条 AI 回复")]}
    mock_llm = _mock_llm("stock")
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await run(state)
    assert result == {"intent": "general"}
    mock_llm.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_supervisor_multimodal_content_not_crash():
    """LLM content 为 list（多模态）→ str() 转换后正常解析，不崩溃"""
    state = {"messages": [HumanMessage(content="分析 600519")]}
    # content 为 list 形态（多模态），str() 后包含 'stock' 关键词
    multimodal_content = [{"type": "text", "text": "stock"}]
    with patch(_GET_QUICK_THINK, return_value=_mock_llm(multimodal_content)):
        result = await run(state)
    assert result["intent"] == "stock"
    assert result["symbol"] == "600519"


@pytest.mark.asyncio
async def test_supervisor_seven_digit_not_matched():
    """连续 7 位数字不被提取为 symbol（\\b(\\d{6})\\b 不匹配 7 位）"""
    state = {"messages": [HumanMessage(content="分析 6005190")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("stock")):
        result = await run(state)
    assert result["intent"] == "stock"
    assert result["symbol"] is None


@pytest.mark.asyncio
async def test_supervisor_letter_sandwiched_digit_not_matched():
    """字母夹数字（600519A）不被提取为 symbol（\\b 在数字与字母间不成立）"""
    state = {"messages": [HumanMessage(content="分析 600519A")]}
    with patch(_GET_QUICK_THINK, return_value=_mock_llm("stock")):
        result = await run(state)
    assert result["intent"] == "stock"
    assert result["symbol"] is None
