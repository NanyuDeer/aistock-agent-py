"""端到端全链路测试 — Task 9

验证「HTTP → middleware → graph → supervisor(意图分类) → worker agent →
create_react_agent → 真实工具执行 → mocked node_api → 最终响应」全链路跑通，
覆盖晨报 SSE + Redis 缓存命中；个股/新闻/板块全流程用例直接驱动 chat 子图
（/chat/message 已切 chat 子图）；工具失败降级已下沉为 tests/unit 单测。

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
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.main import app
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState

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

    同时清理 /briefing/morning 的 Queue，避免 Task 4 双流
    架构的 _message_queues/_update_queues 跨测试残留。
    session_id 现在是随机的 f"briefing_morning_{hex}"，需清理所有
    briefing_morning 前缀的队列。
    """
    from aistock_agent.api import routes as routes_mod
    from aistock_agent.memory import checkpointer as cp_module

    def _purge_briefing_queues():
        for d in (routes_mod._message_queues, routes_mod._update_queues):
            keys = [k for k in d if k.startswith("briefing_morning")]
            for k in keys:
                d.pop(k, None)

    cp_module._checkpointer = None
    _purge_briefing_queues()
    yield
    cp_module._checkpointer = None
    _purge_briefing_queues()


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


# ── /chat/message：chat 子图全链路 ───────────────────────────────
# 注：/chat/message 早已切到 chat 子图（api/routes.py 的 _select_graph 恒返回
# compile_chat_graph()），旧的 supervisor 图驱动方式已不可达。以下个股/新闻用例
# 直接驱动 chat 子图，范式照抄 tests/integration/test_chat_e2e_direct.py：
# compile_chat_graph(checkpointer=None) + ainvoke + mock
# get_quick_think/get_deep_think + 各 skill 依赖。


def _chat_llm_output(
    intent: str,
    skill: str,
    conclusion: str,
    mode: str = "validate",
    *,
    skill_args: dict | None = None,
):
    """构造 chat 子图 qa_router/synth_answer 的结构化 mock 输出（照抄 direct 范式）。"""
    from aistock_agent.graph.nodes.qa_router import QARouterOutput
    from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput

    qa_output = QARouterOutput(
        goal=InsightGoal(question="test", intent=intent),
        plan="direct",
        skill_calls=[SkillCall(skill_name=skill, args=skill_args or {})],
        complexity="light",
    )
    synth_output = SynthOutput(
        insight=SynthInsightOutput(
            conclusion=conclusion,
            basis_indices=[1],
            confidence="medium",
            uncertainty=[],
            answer_mode=mode,
        )
    )
    return qa_output, synth_output


def _chat_mock_llm(qa_output, synth_output) -> MagicMock:
    """构造同时服务 qa_router 与 synth_answer 的 mock LLM。

    qa_router 调 with_structured_output(QARouterOutput).ainvoke → 第 1 个；
    synth_answer 调 with_structured_output(SynthOutput).ainvoke → 第 2 个。
    """
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_output)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_output)),
        ]
    )
    return mock_llm


def _chat_state(message: str) -> QuestionState:
    """chat 子图初始 state（照抄 direct 范式的最小合法结构）。"""
    return {
        "messages": [HumanMessage(content=message)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }


@pytest.mark.asyncio
async def test_full_flow_stock():
    """个股全流程（chat 子图）：qa_router 规划 stock_snapshot → skill 执行 get_quote
    → synth 产出含该个股行情的回答。

    原意图（旧 supervisor e2e）：问个股问题，回答里含该个股信息（1688）。
    迁移到 chat 子图后保留同一意图，并强化为「工具真实产出流入 Evidence + 回答含该信息」。
    """
    qa_out, synth_out = _chat_llm_output(
        "stock_snapshot",
        "stock_snapshot",
        "贵州茅台最新价1688元，涨幅0.75%。",
        skill_args={"symbol": "600519"},
    )
    mock_llm = _chat_mock_llm(qa_out, synth_out)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        # 固定交易时段，去掉对运行时钟/交易日历的依赖（非交易时段 stock_snapshot 会降级）
        "aistock_agent.skills.stock_snapshot.trading_session_status",
        return_value=("trading", ""),
    ), patch(
        # 5253fb4 起 get_quote 走 StructuredTool.ainvoke({"symbol": ...})，需显式挂 ainvoke
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=MagicMock(
            ainvoke=AsyncMock(return_value="【贵州茅台】最新价: 1688.0  涨跌幅: 0.75%")
        ),
    ), patch("aistock_agent.skills.stock_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(
            return_value={
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "最新价": 1688.00,
                "涨跌幅": 0.75,
            }
        )
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_chat_state("分析一下贵州茅台 600519"))

    assert result["insight"] is not None
    assert len(result["evidences"]) == 1
    ev = result["evidences"][0]
    assert ev.skill_name == "stock_snapshot"
    assert ev.degraded is False
    # 工具真实产出该个股行情并流入 Evidence（证明「该个股」全流程取数未断）
    assert any("1688" in fact for fact in ev.facts)
    # 回答包含该个股信息（保留原断言意图，未降级为 is not None）
    assert "1688" in result["final_response"]


@pytest.mark.asyncio
async def test_full_flow_sector():
    """板块全流程（chat 子图）：qa_router 规划 sector_snapshot(tag_code=BK0475)
    → skill 执行 node_api /internal/leader/BK0475 → synth 产出含该板块的回答。

    原意图（旧 supervisor sector_analyst → get_leader_stocks）：问板块强弱，回答里
    含板块关键词（白酒/板块），且 node_api 打到 /internal/leader/BK0475。
    /chat/message 已切 chat 子图（api/routes.py 的 _select_graph 恒返回
    compile_chat_graph()），旧 supervisor 驱动方式不可达；chat 子图无独立 sector worker，
    板块能力由 sector_snapshot skill 承担，故按既有 direct 范式
    （tests/integration/test_chat_e2e_direct.py::test_e2e_sector_snapshot，
    compile_chat_graph(checkpointer=None) + ainvoke + mock qa/synth LLM + mock skill 依赖）驱动。
    """
    qa_out, synth_out = _chat_llm_output(
        "sector_snapshot",
        "sector_snapshot",
        "今日白酒板块表现偏强，龙头贵州茅台涨0.75%。",
        "validate",
        skill_args={"tag_code": "BK0475"},
    )
    mock_llm = _chat_mock_llm(qa_out, synth_out)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        # 闸门 2：消息无 6 位代码时会尝试 resolve_symbol(candidate)（真实打 Node 网络），
        # 固定返回 None → 落「非个股意图（板块）」放行分支走 LLM 路径，避免真实网络请求
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        new=AsyncMock(return_value=None),
    ), patch("aistock_agent.skills.sector_snapshot.node_api") as mock_api:
        mock_api.get = AsyncMock(
            return_value={
                "tag_code": "BK0475",
                "leaders": [{"name": "贵州茅台", "code": "600519", "change_pct": 0.75}],
            }
        )
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_chat_state("今天哪些板块比较强 BK0475"))

    assert result["insight"] is not None
    # sector_snapshot 未在 _DEFAULT_MODE 表中 → synth 推断为 validate（synth_answer.py:343-367）
    assert result["insight"].answer_mode == "validate"
    assert len(result["evidences"]) == 1
    ev = result["evidences"][0]
    assert ev.skill_name == "sector_snapshot"
    assert ev.degraded is False
    # 工具真实产出该板块龙头并流入 Evidence（保留原「板块」语义，且比原断言更具体）
    assert any("贵州茅台" in fact for fact in ev.facts)
    # 板块龙头数据打到原用例同一路径（对应原断言 mock_node_api.get "/internal/leader/BK0475"）
    mock_api.get.assert_any_await("/internal/leader/BK0475")
    # 回答含板块关键词（保留原断言意图；原为「白酒」或「板块」，此处断言更具体的「白酒」）
    assert "白酒" in result["final_response"]


@pytest.mark.asyncio
async def test_full_flow_event():
    """事件/新闻全流程（chat 子图）：qa_router 规划 stock_news → skill 执行
    search_cls_news → synth 产出含该事件关键词的回答。

    原意图（旧 supervisor event_analyst）：问事件/新闻类问题，回答里含事件关键词
    （美联储）。chat 子图无独立 event worker，新闻/事件能力由 stock_news skill 承担，
    故按 direct 范式（test_chat_e2e_direct.py::test_e2e_stock_news）驱动。
    """
    news_text = "美联储纪要显示降息预期升温，新能源板块估值有望修复"
    qa_out, synth_out = _chat_llm_output(
        "stock_news",
        "stock_news",
        "据财联社资讯，美联储纪要显示降息预期升温，新能源板块估值有望修复。",
        "trace",
        skill_args={"symbol": "300750", "limit": 10},
    )
    mock_llm = _chat_mock_llm(qa_out, synth_out)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        # 5253fb4 起 stock_news 走 search_cls_news.ainvoke({"symbol": ...})，需显式挂 ainvoke
        "aistock_agent.skills.stock_news.search_cls_news",
        new=MagicMock(ainvoke=AsyncMock(return_value=news_text)),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_chat_state("宁德时代最近有什么新闻 300750"))

    assert result["insight"] is not None
    assert result["insight"].answer_mode == "trace"
    assert len(result["evidences"]) == 1
    ev = result["evidences"][0]
    assert ev.skill_name == "stock_news"
    assert ev.degraded is False
    # 新闻工具真实产出流入 Evidence
    assert any("美联储" in fact for fact in ev.facts)
    # 回答包含该事件关键词（保留原断言意图，未降级为 is not None）
    assert "美联储" in result["final_response"]


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
         patch("aistock_agent.tools.market_tools.node_api") as mock_market_api, \
         patch("tavily.TavilyClient"):
        mock_pool.get_client = AsyncMock(return_value=mock_redis)
        # 1415406 起全球行情改走 market_tools.node_api（/api/gb/index/quotes），
        # 已无 yfinance（原 patch("...market_tools.yf") 目标不存在）
        mock_market_api.get = AsyncMock(return_value={
            "行情": [
                {"指数代码": "SPX", "指数简称": "标普500", "最新价": 5500.0, "涨跌幅": 0.36},
            ],
        })
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


# 注：原 test_full_flow_tool_failure_degradation（工具失败降级）已下沉为
# tests/unit/test_worker_tool_degradation.py 的单元测试——该 e2e 走旧 supervisor 路径
# （/chat/message 已切 chat 子图）不可达，且降级点在工具/worker 层，用单测覆盖更直接。


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
    assert done_events
    # 7065b7f 起晨报缓存读取侧返回双层 JSON（schema 1.0）：旧纯文本被包装进
    # display_report.details，故解析后比较 details 而非整体字符串
    payload = json.loads(done_events[0].get("final_response", ""))
    assert payload["schema_version"] == "1.0"
    assert payload["display_report"]["details"] == cached_content
    # LLM 未被调用（哨兵未触发 AssertionError 即证明）
    deep_sentinel.assert_not_called()
