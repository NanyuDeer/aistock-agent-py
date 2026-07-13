"""端到端全链路测试 — Task 9

验证「HTTP → middleware → graph → supervisor(意图分类) → worker agent →
create_react_agent → 真实工具执行 → mocked node_api → 最终响应」全链路跑通，
覆盖 5 类意图 + 工具失败降级 + Redis 缓存命中。

与现有测试的层次区别（互补，不重复）：
- ``tests/e2e/test_chat_message.py``：mock 各 agent.run —— 验证路由 + HTTP 契约，
  但不执行工具（create_react_agent 未被调用）。
- ``tests/integration/test_*_agent.py``：mock create_react_agent —— 验证 agent
  逻辑与工具绑定，但不执行工具。
- **本文件**：mock LLM（``get_quick_think``/``get_deep_think``）+ node_api，
  让真实 ``create_react_agent`` + 真实 ``@tool`` 函数执行，验证「工具被实际调用、
  node_api 被打到正确路径、工具结果回流到最终响应」。

mock 策略：
- **LLM**：``FakeToolCallingLLM``（``BaseChatModel`` 子类），按序列返回预置
  ``AIMessage``：先返回带 ``tool_calls`` 的消息触发工具调用，再返回纯文本作为
  最终回复。``bind_tools`` 返回 self 以兼容 ``create_react_agent``；同时实现
  ``_stream`` 使 ``astream_events`` 能产出 ``on_chat_model_stream``（晨报 SSE 需要）。
- **node_api**：patch 各 tool 模块的 ``node_api``（``from ... import node_api``
  在 import 时复制引用，必须 patch 消费方模块而非源模块）。
- **Redis**：复用 conftest 思路 patch ``services.cache.RedisPool``（缓存命中测试）。
- **HTTP**：``httpx.AsyncClient(transport=httpx.ASGITransport(app=app))``，
  lifespan 不运行 → RedisPool/HttpClientPool 未初始化，故必须 mock。

测试不调用真实 LLM API（零 token 消耗）、不依赖外部服务（Redis/Node.js 全 mock）。
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.main import app

_CHAT_URL = "/api/agent/chat/message"
_BRIEFING_URL = "/api/agent/briefing/morning"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


# ── FakeToolCallingLLM ────────────────────────────────────────────


class FakeToolCallingLLM(BaseChatModel):
    """按序列返回预置 AIMessage 的假 LLM，兼容 create_react_agent。

    - ``bind_tools`` 返回 self（``create_react_agent`` 要求模型支持 bind_tools；
      ``FakeMessagesListChatModel.bind_tools`` 默认 raise NotImplementedError，
      故必须子类化并覆写）。
    - ``_generate``：供 ``ainvoke`` 路径（/chat/message 各 worker agent）使用，
      每次调用弹出 ``responses`` 中的下一条消息。
    - ``_stream``：供 ``astream_events`` 路径（/briefing/morning SSE）使用，
      把 AIMessage 转为 ``AIMessageChunk`` 产出。``tool_call_chunks`` 要求
      ``args`` 为 JSON 字符串（langchain pydantic 校验），故对 tool_calls 做序列化。
    - ``_idx`` 用 ``PrivateAttr`` 持有调用计数器（pydantic 模型私有属性）。

    用法：构造时传入 ``responses=[AIMessage(tool_calls=[...]), AIMessage(content="最终回复")]``。
    """

    responses: list[AIMessage] = []
    _idx: int = PrivateAttr(default=0)

    def _next(self) -> AIMessage:
        msg = self.responses[self._idx] if self._idx < len(self.responses) else self.responses[-1]
        self._idx += 1
        return msg

    def _generate(
        self,
        messages: object,
        stop: list[str] | None = None,
        run_manager: object = None,
        **kwargs: object,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(
        self,
        messages: object,
        stop: list[str] | None = None,
        run_manager: object = None,
        **kwargs: object,
    ):
        msg = self._next()
        tcc: list[dict[str, object]] = []
        for i, tc in enumerate(msg.tool_calls or []):
            tcc.append(
                {
                    "name": tc["name"],
                    "args": json.dumps(tc.get("args", {}), ensure_ascii=False),
                    "id": tc.get("id", f"call_{i}"),
                    "index": i,
                    "type": "tool_call_chunk",
                }
            )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=msg.content, tool_call_chunks=tcc)
        )

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def bind_tools(self, tools: object, **kwargs: object) -> FakeToolCallingLLM:
        """返回 self —— 假模型不需要真正绑定工具 schema。"""
        return self


def _tc(name: str, args: dict[str, object], call_id: str = "call_1") -> AIMessage:
    """构造一条带 tool_calls 的 AIMessage（content 为空，仅触发工具调用）。"""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _text(content: str) -> AIMessage:
    """构造一条纯文本最终回复 AIMessage（无 tool_calls，结束 ReAct 循环）。"""
    return AIMessage(content=content)


# ── 公共 fixture ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_checkpointer():
    """每个测试前重置 checkpointer 单例，避免跨测试 MemorySaver checkpoint 残留。

    与 tests/integration/test_graph.py 保持一致。compile_graph() 复用单例
    MemorySaver；不同 thread_id 已隔离数据，这里再重置仅为测试卫生。

    同时清理 /briefing/morning 固定 session_id 的 Queue，避免 Task 4 双流
    架构的 _event_queues 跨测试残留。
    """
    from aistock_agent.api import routes as routes_mod
    from aistock_agent.memory import checkpointer as cp_module

    cp_module._checkpointer = None
    routes_mod._event_queues.pop("briefing_morning", None)
    yield
    cp_module._checkpointer = None
    routes_mod._event_queues.pop("briefing_morning", None)


# 被 5 个 agent 用到的、走 node_api 的 tool 模块（market_tools 走 yfinance/tavily，单独 mock）
_NODE_API_TOOL_MODULES = (
    "aistock_agent.tools.stock_tools",
    "aistock_agent.tools.sector_tools",
    "aistock_agent.tools.news_tools",
)


@pytest.fixture
def mock_node_api():
    """patch 所有走 node_api 的 tool 模块的 ``node_api``，返回共享 mock。

    ``from ... import node_api`` 在 import 时把单例引用复制到各 tool 模块，
    故必须 patch 消费方模块（而非 ``services.data_client.node_api`` 源）。
    共享同一个 mock 实例，便于按 path 配置 ``get`` 的 side_effect。
    """
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.get_list = AsyncMock(return_value=None)
    with ExitStack() as stack:
        for mod in _NODE_API_TOOL_MODULES:
            stack.enter_context(patch(f"{mod}.node_api", new=mock))
        yield mock


def _patch_llms(
    quick_responses: list[AIMessage] | None = None,
    deep_responses: list[AIMessage] | None = None,
) -> tuple[ExitStack, FakeToolCallingLLM, FakeToolCallingLLM]:
    """patch supervisor/general 的 get_quick_think 与各 worker 的 get_deep_think。

    supervisor 与 general_agent 都用 get_quick_think，但分属不同模块、顺序调用，
    故 patch 两个模块路径返回 **同一个** FakeLLM 实例，让计数器顺序消费。
    各 worker（stock/sector/event/morning）的 get_deep_think 同理返回同一个 deep 实例。

    Returns:
        (exit_stack, quick_llm, deep_llm) —— 测试可在 with 块内进一步断言。
    """
    quick = FakeToolCallingLLM(responses=quick_responses or [])
    deep = FakeToolCallingLLM(responses=deep_responses or [])
    stack = ExitStack()
    # quick_think：supervisor + general_agent + event(understanding/history/investment/podcast)
    # 顺序消费同一实例
    stack.enter_context(
        patch("aistock_agent.agents.supervisor.node.get_quick_think", return_value=quick)
    )
    stack.enter_context(
        patch("aistock_agent.agents.general.node.get_quick_think", return_value=quick)
    )
    stack.enter_context(
        patch("aistock_agent.agents.workers.event.get_quick_think", return_value=quick)
    )
    # deep_think：各 worker 模块顺序消费同一实例
    for mod in (
        "aistock_agent.agents.workers.stock",
        "aistock_agent.agents.workers.sector",
        "aistock_agent.agents.workers.event",
    ):
        stack.enter_context(patch(f"{mod}.get_deep_think", return_value=deep))
    return stack, quick, deep


# ── /chat/message：5 类意图全链路 ─────────────────────────────────


@pytest.mark.asyncio
async def test_full_flow_stock(mock_node_api):
    """stock 意图全链路：supervisor 分类 → stock_analyst ReAct → get_quote 真实执行。

    验证三件事（区别于 mock agent.run 的 test_chat_message.py）：
    1. ``create_react_agent`` 真实运行、``get_quote`` 真实执行（非 mock agent）；
    2. node_api 被打到 ``/internal/quote/600519``（证明 LLM→tool→node_api 链路未断）；
    3. 工具结果回流，最终响应含行情信息。
    """
    mock_node_api.get.return_value = {
        "股票简称": "贵州茅台",
        "最新价": 1688.00,
        "涨跌幅": 0.75,
    }
    with _patch_llms(
        quick_responses=[_text("stock")],  # supervisor 意图分类
        deep_responses=[  # stock_analyst ReAct：先调 get_quote，再给最终回复
            _tc("get_quote", {"symbol": "600519"}),
            _text("贵州茅台最新价1688元，涨幅0.75%，主力资金呈净流出。"),
        ],
    )[0]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "分析一下贵州茅台 600519", "session_id": "e2e-stock"},
                headers=_VALID_HEADERS,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "1688" in body["content"]
    assert body["session_id"] == "e2e-stock"
    # 关键断言：工具真实执行，node_api 被打到 /internal/quote/600519
    mock_node_api.get.assert_any_await("/internal/quote/600519")


@pytest.mark.asyncio
async def test_full_flow_sector(mock_node_api):
    """sector 意图全链路：supervisor → sector_analyst → get_leader_stocks。

    验证：tag_code 从用户消息提取（BK0475），node_api 打到
    ``/internal/leader/BK0475``，最终响应含板块分析。
    """
    mock_node_api.get.return_value = {
        "tag_code": "BK0475",
        "leaders": [{"name": "贵州茅台", "code": "600519", "change_pct": 0.75}],
    }
    with _patch_llms(
        quick_responses=[_text("sector")],
        deep_responses=[
            _tc("get_leader_stocks", {"tag_code": "BK0475"}),
            _text("白酒板块今日表现偏强，龙头贵州茅台涨0.75%。"),
        ],
    )[0]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "今天哪些板块比较强 BK0475", "session_id": "e2e-sector"},
                headers=_VALID_HEADERS,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "白酒" in body["content"] or "板块" in body["content"]
    mock_node_api.get.assert_any_await("/internal/leader/BK0475")


@pytest.mark.asyncio
async def test_full_flow_event(mock_node_api):
    """event 意图全链路：supervisor → event_analyst v3 → get_news_fulltext。

    v3 拆分为 5 个 LLM 调用：
    - Call 1 understanding (flash): 事件理解 JSON
    - Call 2 transmission (deep, ReAct): 调 get_news_fulltext → 传导 JSON
    - Call 3 history (flash, ReAct): 历史 JSON 数组（无工具调用）
    - Call 4 investment (flash): 投资建议 JSON
    - Call 5 podcast (flash): 播报文本

    quick LLM 消费顺序：supervisor → understanding → history → investment → podcast
    deep LLM 消费顺序：transmission(ReAct tool_call) → transmission(ReAct final JSON)
    """
    mock_node_api.get.return_value = {
        "title": "美联储维持利率不变",
        "content": "美联储7月议息会议决定维持联邦基金利率目标区间不变。",
    }
    _understanding = json.dumps({
        "summary": "美联储维持利率不变",
        "coreChanges": [{"variable": "利率", "before": "不确定", "after": "维持不变"}],
    })
    _transmission = json.dumps({
        "mechanism": "利率维持不变，市场流动性预期稳定",
        "variables": [{"name": "利率", "direction": "neutral", "strength": 0.5,
                        "explanation": "维持不变"}],
        "coreIndustry": {"name": "科技", "impact": "中性偏正", "reason": "低利率环境利好成长股"},
        "chain": [{"industry": "科技", "relation": "核心行业", "level": 1,
                    "direction": "bullish", "impactStrength": 0.6, "reason": "低利率利好"}],
    })
    _history = json.dumps([{
        "historyId": "h001", "year": "2024", "title": "上次维持利率",
        "eventType": "市场动态", "sentiment": "neutral",
        "industryChange": "市场波动不大", "changePercentage": 0.5,
    }])
    _investment = json.dumps({
        "conclusion": "美联储维持利率不变，A股整体偏中性，关注科技板块。",
        "keyPoints": ["利率不变"],
        "focusIndustries": [{"name": "科技", "direction": "positive",
                              "reason": "利率稳定利好成长股"}],
        "opportunities": ["科技板块"],
        "risks": ["外部不确定性"],
        "rating": "neutral",
    })
    with _patch_llms(
        quick_responses=[
            _text("event"),           # supervisor 意图分类
            _text(_understanding),     # Call 1: understanding (flash, no tools)
            _text(_history),           # Call 3: history (flash, ReAct, no tool calls)
            _text(_investment),        # Call 4: investment (flash, no tools)
            _text("美联储维持利率不变，对A股整体偏中性，关注科技板块。"),  # Call 5: podcast
        ],
        deep_responses=[
            _tc("get_news_fulltext", {"news_id": "20260708"}),  # Call 2: transmission ReAct
            _text(_transmission),     # Call 2: transmission final JSON
        ],
    )[0]:
        with patch("aistock_agent.agents.workers.event.get_cached_event",
                   AsyncMock(return_value=None)):
            with patch("aistock_agent.agents.workers.event.set_cached_event", AsyncMock()):
                with patch("aistock_agent.agents.workers.event.persist_event_report",
                           AsyncMock()):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test",
                    ) as client:
                        resp = await client.post(
                            _CHAT_URL,
                            json={"message": "分析美联储加息的影响", "session_id": "e2e-event"},
                            headers=_VALID_HEADERS,
                        )

    assert resp.status_code == 200
    body = resp.json()
    assert "美联储" in body["content"]
    mock_node_api.get.assert_any_await("/internal/news/fulltext/20260708")


@pytest.mark.asyncio
async def test_full_flow_general():
    """general 意图全链路：supervisor → general_agent（quick_think，无工具调用）。

    general_agent 用 get_quick_think，与 supervisor 共享同一 FakeLLM 实例：
    supervisor 消费 responses[0]（意图），general 消费 responses[1]（最终回复）。
    验证兜底路径返回正常回复（非错误）。
    """
    with _patch_llms(
        quick_responses=[
            _text("general"),  # supervisor 意图分类
            _text("你好！我是AI投资助手，可以帮你分析个股、板块和市场动态。"),  # general 回复
        ],
    )[0]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "你好", "session_id": "e2e-general"},
                headers=_VALID_HEADERS,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "助手" in body["content"] or "你好" in body["content"]


# ── /briefing/morning：晨报 SSE 全链路 ────────────────────────────


def _parse_sse(text: str) -> list[dict[str, object]]:
    """解析 SSE 响应文本为事件列表（每行 ``data: {json}``）。"""
    events: list[dict[str, object]] = []
    for line in text.split("\n"):
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


async def _read_sse(resp: httpx.Response) -> str:
    text = ""
    async for line in resp.aiter_lines():
        text += line + "\n"
    return text


@pytest.mark.asyncio
async def test_full_flow_morning(mock_node_api):
    """晨报 SSE 全链路：graph → supervisor(意图分类) → morning_agent → get_cls_news 真实执行。

    Task 4 重构后 /briefing/morning 走 graph 转发（_stream_messages, filter_type="text"），
    SSE 事件序列含 text/done（tool 事件被过滤，在 /chat/stream/updates 中）。
    需额外 patch supervisor 的 get_quick_think 以分类意图为 "morning"。
    """
    mock_node_api.get.return_value = {
        "items": [{"title": "美联储维持利率不变", "time": "2026-07-08"}],
    }
    # supervisor 意图分类 LLM
    quick = FakeToolCallingLLM(responses=[_text("morning")])
    # morning_agent ReAct LLM
    deep = FakeToolCallingLLM(
        responses=[
            _tc("get_cls_news", {}, call_id="m1"),
            _text("今日晨报：美联储维持利率不变，A股有望震荡偏强，关注科技与消费板块。"),
        ]
    )
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # 缓存未命中
    mock_redis.setex = AsyncMock()

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool, \
         patch("aistock_agent.agents.supervisor.node.get_quick_think", return_value=quick), \
         patch("aistock_agent.agents.workers.morning.get_deep_think", return_value=deep), \
         patch("aistock_agent.agents.workers.morning.is_trading_day", return_value=True), \
         patch("aistock_agent.tools.market_tools.yf"), \
         patch("tavily.TavilyClient"):
        mock_pool.get_client = AsyncMock(return_value=mock_redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            async with client.stream("GET", _BRIEFING_URL) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                text = await _read_sse(resp)

    events = _parse_sse(text)
    types = [e.get("type") for e in events]
    # 新双流架构：/briefing/morning 走 _stream_messages (filter_type="text")
    # messages 流只含 text + done，tool 事件被过滤
    assert SSEEventType.TEXT in types, f"missing text, got {types}"
    assert types[-1] == SSEEventType.DONE, f"last should be done, got {types}"
    assert SSEEventType.TOOL_START not in types
    # done 携带 final_response
    done_events = [e for e in events if e.get("type") == SSEEventType.DONE]
    assert done_events and "晨报" in done_events[0].get("final_response", "")
    # 工具真实执行：node_api 打到 /internal/news/latest
    mock_node_api.get.assert_any_await("/internal/news/latest?limit=10")


# ── 异常路径：工具失败降级 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_flow_tool_failure_degradation(mock_node_api):
    """工具失败降级：node_api.get 抛异常 → safe_tool_call 返回降级文本，agent 不崩溃。

    模拟 /internal/quote 返回 500（node_api._request 捕获 HTTPStatusError 后返回 None，
    这里直接让 mock.get 抛 RuntimeError 模拟底层异常被 safe_tool_call 捕获）。
    验证：HTTP 仍返回 200（非 500），响应为降级文本而非报错。
    """
    mock_node_api.get.side_effect = RuntimeError("upstream 500")

    with _patch_llms(
        quick_responses=[_text("stock")],
        deep_responses=[
            _tc("get_quote", {"symbol": "600519"}),
            _text("行情数据暂不可用，请稍后重试。"),
        ],
    )[0], patch("aistock_agent.tools.market_tools.yf"), patch("tavily.TavilyClient"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "分析 600519", "session_id": "e2e-degrade"},
                headers=_VALID_HEADERS,
            )

    # 关键断言：工具失败不传播为 HTTP 500，agent 仍正常响应（降级文本）
    assert resp.status_code == 200
    body = resp.json()
    assert "暂不可用" in body["content"]
    # 工具确实被尝试调用过（失败发生在工具内部，被 safe_tool_call 捕获）
    mock_node_api.get.assert_any_await("/internal/quote/600519")


# ── 缓存路径：Redis 缓存命中 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_full_flow_redis_cache_hit():
    """Redis 缓存命中：morning_agent.run 直接返回缓存，不调用 deep LLM。

    Task 4 重构后 /briefing/morning 走 graph 转发。缓存命中时 morning_agent.run
    直接返回缓存内容（不调 LLM），supervisor 的 text 事件被过滤（node=="supervisor"），
    SSE 输出仅 done 事件（携带 final_response = 缓存内容）。
    需额外 patch supervisor 的 get_quick_think 以分类意图为 "morning"。
    """
    cached_content = "缓存晨报：昨日市场震荡收涨，今日关注数据发布。"
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=cached_content.encode("utf-8"))
    mock_redis.setex = AsyncMock()

    # supervisor 意图分类 LLM
    quick = FakeToolCallingLLM(responses=[_text("morning")])
    # 哨兵：缓存命中时 get_deep_think 不应被调用
    deep_sentinel = MagicMock(side_effect=AssertionError("LLM must not be called on cache hit"))

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool, \
         patch("aistock_agent.agents.supervisor.node.get_quick_think", return_value=quick), \
         patch("aistock_agent.agents.workers.morning.get_deep_think", new=deep_sentinel), \
         patch("aistock_agent.agents.workers.morning.is_trading_day", return_value=True):
        mock_pool.get_client = AsyncMock(return_value=mock_redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            async with client.stream("GET", _BRIEFING_URL) as resp:
                assert resp.status_code == 200
                text = await _read_sse(resp)

    events = _parse_sse(text)
    types = [e.get("type") for e in events]
    # 缓存命中：仅 done 事件，无 tool/text（supervisor text 被过滤，morning 未调 LLM）
    assert types[-1] == SSEEventType.DONE
    assert SSEEventType.TOOL_START not in types
    # done 携带 final_response（缓存内容）
    done_events = [e for e in events if e.get("type") == SSEEventType.DONE]
    assert done_events and done_events[0].get("final_response") == cached_content
    # LLM 未被调用（哨兵未触发 AssertionError 即证明）
    deep_sentinel.assert_not_called()
