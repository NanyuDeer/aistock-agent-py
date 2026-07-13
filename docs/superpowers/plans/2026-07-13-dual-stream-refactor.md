# 双流 SSE 重构 + morning stream 合并 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use Skill(name="subagent-driven-development") (recommended) or Skill(name="executing-plans") to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将用户对话 chat 从非流式 `ainvoke` 改为双流 SSE（messages 逐 token + updates 工具进度），同时合并 morning 的 `stream()` 到 `run()` 消除重复代码。

**Architecture:** 路由层新增 `asyncio.Queue` 扇出机制：graph 只执行一次，messages/updates 两个 SSE generator 从共享 Queue 读事件、按 `filter_type` 分派。`/briefing/morning` 改为 graph 转发（复用 messages generator）。agent 层不动。

**Tech Stack:** Python 3.12+, asyncio.Queue, LangGraph astream_events(v2), FastAPI + sse-starlette, pytest-asyncio

## Spec Self-Review 修复项

对设计文档的 `_stream_messages` 实现做了一处修正：原设计 `should_start = session_id not in _event_queues` 放在 `_get_or_create_queue()` 之后调用，此时 Queue 已存在导致 `should_start` 恒为 `False`。实施计划中修改为在 `_get_or_create_queue()` 之前检查，且 `_get_or_create_queue` 改为返回 `(Queue, is_new)` 元组。

## Global Constraints

- 不改动 `agents/workers/event.py` / `review.py` / `stock.py` / `sector.py` / `iterate.py`
- 不改动 `graph/builder.py` / `services/cache.py` / `services/scheduler.py`
- `/chat/message` 保留不删，仅 docstring 标记 @deprecated
- 类型检查：mypy strict 必须通过
- 代码检查：ruff 必须通过
- 330+ 现有测试必须全部通过（无回归）

---

### Task 1: constants.py — 新增 SSE 事件类型

**Files:**
- Modify: `src/aistock_agent/constants.py:9-21`

**Interfaces:**
- Produces: `SSEEventType.AGENT_SWITCH = "agent_switch"`, `SSEEventType.INTERMEDIATE = "intermediate"`
- Consumed by: Task 4 (routes.py uses AGENT_SWITCH)

- [ ] **Step 1: Add two new event types to SSEEventType**

```python
class SSEEventType:
    """前端 SSE 事件类型常量（字符串常量类，避免 enum 复杂度）。

    被 ``utils.sse.map_langgraph_event_to_sse`` 与 ``agents.workers.morning.stream``
    引用，禁止在业务代码中写 magic string。
    """

    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    LLM_START = "llm_start"
    TEXT = "text"
    DONE = "done"
    ERROR = "error"
    AGENT_SWITCH = "agent_switch"
    INTERMEDIATE = "intermediate"
```

- [ ] **Step 2: Run ruff to verify**

Run: `ruff check src/aistock_agent/constants.py`
Expected: PASS (no errors)

- [ ] **Step 3: Commit**

```powershell
git add src/aistock_agent/constants.py
git commit -m "feat(constants): add AGENT_SWITCH and INTERMEDIATE SSE event types"
```

---

### Task 2: utils/sse.py — 加 filter_type 分流参数

**Files:**
- Modify: `src/aistock_agent/utils/sse.py:1-58`
- Test: `tests/unit/test_utils_sse.py` (existing, verify no regression)

**Interfaces:**
- Modifies: `map_langgraph_event_to_sse(event, filter_type="all") -> dict | None`
- `filter_type="text"` → 跳过 on_tool_start/on_tool_end
- `filter_type="tool"` → 跳过 on_chat_model_stream
- `filter_type="all"` → 原行为不变
- Consumed by: Task 4 (routes.py)

- [ ] **Step 1: Add filter_type parameter**

Modify the function signature and add filtering logic in `src/aistock_agent/utils/sse.py`:

```python
"""SSE 事件映射 — LangGraph ``astream_events`` → 前端 SSE 事件。

从 ``agents.workers.morning.stream`` 抽出无状态的单事件转换逻辑。
``map_langgraph_event_to_sse`` 返回 ``None`` 表示该事件应被过滤掉（如带
tool_calls 的 chunk）。

注意：``llm_start`` 的"仅首次发射"逻辑是有状态的，仍由调用方（routes.py
generator）维护 ``_llm_started`` 标志；本函数只负责单个 LangGraph 事件 →
SSE 事件的转换。
"""

from collections.abc import Mapping
from typing import Any, Literal

from aistock_agent.constants import TOOL_LABELS, LangGraphEventType, SSEEventType

# 合法的 filter_type 字面量
FilterType = Literal["all", "text", "tool"]


def map_langgraph_event_to_sse(
    event: Mapping[str, Any],
    filter_type: FilterType = "all",
) -> dict[str, object] | None:
    """将单个 LangGraph 事件映射为 SSE 事件 dict。

    Args:
        event: ``astream_events(version="v2")`` 产出的单个事件 dict。
        filter_type: 事件分流模式。
            - ``"all"``（默认）：不过滤，返回所有有效事件。
            - ``"text"``：仅返回 text chunk 事件，跳过工具事件。
            - ``"tool"``：仅返回工具事件，跳过 text chunk 事件。

    Returns:
        SSE 事件 dict（含 ``type`` 键），或 ``None`` 表示该事件应被过滤。
    """
    event_type = event.get("event")
    tool_name = event.get("name", "")

    if event_type == LangGraphEventType.ON_TOOL_START:
        if filter_type == "text":
            return None
        label = TOOL_LABELS.get(tool_name, tool_name)
        sse_event: dict[str, object] = {
            "type": SSEEventType.TOOL_START,
            "tool": tool_name,
            "label": label,
        }
        query = event.get("data", {}).get("input", {}).get("query")
        if query:
            sse_event["args"] = {"query": query}
        return sse_event

    if event_type == LangGraphEventType.ON_TOOL_END:
        if filter_type == "text":
            return None
        return {"type": SSEEventType.TOOL_END, "tool": tool_name}

    if event_type == LangGraphEventType.ON_CHAT_MODEL_STREAM:
        if filter_type == "tool":
            return None
        chunk = event.get("data", {}).get("chunk")
        if not chunk:
            return None
        has_text = bool(chunk.content)
        has_tool_calls = bool(
            getattr(chunk, "tool_calls", None)
            or getattr(chunk, "tool_call_chunks", None)
        )
        # 仅产出纯文本 chunk，带 tool_calls 的 chunk（函数调用中间态）过滤掉
        if has_text and not has_tool_calls:
            return {"type": SSEEventType.TEXT, "content": chunk.content}
        return None

    return None
```

- [ ] **Step 2: Verify no regression with existing tests**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/unit/test_utils_sse.py -v`
Expected: All 8 tests PASS (default `"all"` mode preserves existing behavior)

- [ ] **Step 3: Run ruff + mypy**

Run: `ruff check src/aistock_agent/utils/sse.py`
Expected: PASS

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/utils/sse.py`
Expected: PASS

- [ ] **Step 4: Commit**

```powershell
git add src/aistock_agent/utils/sse.py
git commit -m "feat(sse): add filter_type param to map_langgraph_event_to_sse for dual-stream routing"
```

---

### Task 3: morning.py — 删除 stream()，run() 加非交易日判断

**Files:**
- Modify: `src/aistock_agent/agents/workers/morning.py:1-152`
- Modify: `tests/integration/test_morning_agent.py` (delete stream() tests, update run() test)
- Modify: `tests/integration/test_agent_fallback.py` (update morning fallback test if needed)

**Interfaces:**
- Removes: `morning.stream(state) -> AsyncGenerator`
- Preserves: `morning.run(state) -> dict`
- run() gains: `is_trading_day()` check in system prompt construction
- Consumers (unchanged): `graph/builder.py` (via `morning_agent.run`), `services/scheduler.py`

- [ ] **Step 1: Delete stream() function and unused imports from morning.py**

Read current `morning.py`, then write the new version:

```python
"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时（通过 services.cache → RedisPool 单例）
归档：docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md

流式：由 graph 层 ``astream_events(v2)`` 自动提供，agent 不关心传输协议。
"""

from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.morning import MORNING_PROMPT
from aistock_agent.services.archiver import archive_morning
from aistock_agent.services.cache import get_cached_briefing, set_cached_briefing
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.date import is_trading_day
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.output_parser import extract_major_events

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """晨报分析：宏观策略4步框架 — cache → create_react_agent → ainvoke → extract → cache+archive

    流式由 graph 层 ``astream_events(v2)`` 提供，agent 不关心传输协议。
    """
    try:
        today = datetime.now().strftime("%Y年%m月%d日")

        # 检查缓存
        cached = await get_cached_briefing()
        if cached:
            major_events = extract_major_events(cached)
            return {
                "final_response": cached,
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "major_events": major_events,
                },
            }

        # 构建提示词
        system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
        if not is_trading_day():
            system_prompt += (
                "\n\n注意：今日为非交易日（周末或节假日），"
                "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
            )

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("morning")
        agent = create_react_agent(llm, tools)

        # 执行
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
        )

        # 提取最终响应
        final_response = extract_final_ai_response(result.get("messages", []))

        # 提取 major_events（供 event agent 消费）
        major_events = extract_major_events(final_response)
        if major_events:
            logger.info(
                "morning_major_events_extracted",
                count=len(major_events),
                titles=[str(e.get("title", ""))[:30] for e in major_events],
            )

        # 缓存 + 归档（供 snapshot_builder 读取）
        if final_response:
            await set_cached_briefing(final_response)
            archive_morning(final_response)

        return {
            "final_response": final_response,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "major_events": major_events,
            },
        }
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="morning",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "晨报生成暂时不可用，请稍后重试"}
```

- [ ] **Step 2: Delete stream() tests from test_morning_agent.py**

Remove the following test functions from `tests/integration/test_morning_agent.py`:
- `test_stream_cache_hit` (lines 42-52)
- `test_stream_tool_events_mapped` (lines 55-85)
- `test_stream_filters_tool_call_chunks` (lines 88-123)
- `test_stream_non_trading_day_injects_prompt` (lines 126-152)
- `_async_iter` helper function (lines 36-39)

Also remove the `mock_redis` fixture import and `_async_iter` helper if no longer used. Remove imports:
- `from unittest.mock import AsyncMock, MagicMock, patch` → keep `AsyncMock, MagicMock, patch` (still used by run() tests)

- [ ] **Step 3: Add non-trading-day test for run()**

Add to `tests/integration/test_morning_agent.py` after existing run() tests:

```python
@pytest.mark.asyncio
async def test_morning_run_non_trading_day_injects_prompt():
    """非交易日时 system_prompt 包含非交易日提示。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="晨报")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        with patch("aistock_agent.agents.workers.morning.is_trading_day", return_value=False):
                            await morning_agent.run({})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "非交易日" in messages[0].content

# 需要新增的 import（如果 test 文件中还没有）：
# from langchain_core.messages import AIMessage, SystemMessage  → 已有
# from unittest.mock import MagicMock                          → 已有
```

- [ ] **Step 4: Run morning tests**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/integration/test_morning_agent.py -v`
Expected: All run() tests PASS, no stream() tests present

- [ ] **Step 5: Run ruff + mypy on morning.py**

Run: `ruff check src/aistock_agent/agents/workers/morning.py`
Expected: PASS

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/agents/workers/morning.py`
Expected: PASS

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/ -v --ignore=tests/integration/test_morning_agent.py -k "not morning" 2>&1 | Select-Object -Last 20`

Wait — that's too complex. Just run the full suite:

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/ -v 2>&1`
Expected: All tests PASS except pre-existing failures (same 3 pre-existing failures: test_log_level_exists, test_intent_set_contents, test_sector_agent_tools_bound_correctly)

- [ ] **Step 7: Commit**

```powershell
git add src/aistock_agent/agents/workers/morning.py tests/integration/test_morning_agent.py
git commit -m "refactor(morning): delete stream(), merge into run() with is_trading_day check"
```

---

### Task 4: routes.py — 双流端点 + Queue 扇出 + /briefing/morning graph 转发

**Files:**
- Modify: `src/aistock_agent/api/routes.py:1-225` (full rewrite of streaming section)
- Test: `tests/integration/test_dual_stream.py` (new file)

**Interfaces:**
- Produces: `POST /chat/stream/messages`, `POST /chat/stream/updates`
- Modifies: `GET /briefing/morning` (now delegates to graph)
- Modifies: `POST /chat/message` (docstring @deprecated only)
- Consumes: `map_langgraph_event_to_sse` with `filter_type` (Task 2), `SSEEventType.AGENT_SWITCH` (Task 1)

- [ ] **Step 1: Write the new routes.py**

Replace the entire streaming section of `routes.py`. The existing imports block stays the same (lines 1-19), only the streaming routes change.

Current structure to keep:
- Lines 1-19: imports (unchanged)
- Lines 20-25: router/health_router setup (unchanged)
- Lines 27-50: `chat_message` (add @deprecated docstring)
- Lines 53-111: `/chat/stream` (REPLACE entirely)
- Lines 114-139: `/briefing/morning` (REPLACE entirely)
- Lines 142-225: skills + health (unchanged)

Here is the complete replacement for the streaming section:

```python
import asyncio
import json
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Depends, Response
from sse_starlette.sse import EventSourceResponse

from aistock_agent.api.deps import build_initial_state, verify_internal_token
from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.graph.builder import compile_graph
from aistock_agent.schemas.chat import ChatRequest, ChatResponse
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.utils.sse import map_langgraph_event_to_sse

router = APIRouter()

# 健康检查路由（在 main.py 挂载到根路径，不在 /api/agent 前缀下）
health_router = APIRouter(tags=["health"])
_health_logger = structlog.get_logger()

# ── session_id → asyncio.Queue 扇出映射 ──
# messages 流和 updates 流共享同一个 graph 执行结果，graph 只跑一次。
_event_queues: dict[str, asyncio.Queue] = {}


def _ensure_queue(session_id: str) -> tuple[asyncio.Queue, bool]:
    """获取或创建 session 对应的事件队列。

    Returns:
        (queue, is_new): queue 实例 + 是否为新创建（首次调用时为 True，
        后续 updates 连接时为 False）。
    """
    is_new = session_id not in _event_queues
    if is_new:
        _event_queues[session_id] = asyncio.Queue()
    return _event_queues[session_id], is_new


async def _run_graph_to_queue(graph, initial_state, session_id):
    """后台执行 graph，所有 ``astream_events`` 事件推入共享 Queue。

    仅由 messages generator 的首次连接触发，updates generator 不启动。
    """
    queue, _ = _ensure_queue(session_id)
    try:
        async for event in graph.astream_events(
            initial_state, version="v2",
            config={"configurable": {"thread_id": session_id}},
        ):
            await queue.put(event)
    except Exception as exc:
        await queue.put({"__error__": str(exc)})
    finally:
        await queue.put(None)  # 哨兵：事件流结束
        _event_queues.pop(session_id, None)


async def _stream_messages(graph, initial_state, session_id):
    """messages 流 — 从共享 Queue 读事件，发射 TEXT + LLM_START + DONE"""
    queue, is_new = _ensure_queue(session_id)

    # 首次 messages 连接触发 graph 执行，updates 连接只读
    if is_new:
        asyncio.create_task(_run_graph_to_queue(graph, initial_state, session_id))

    _llm_started = False
    try:
        while True:
            event = await queue.get()
            if event is None:
                # graph 结束 → 取后处理结果
                final_state = await graph.aget_state(
                    config={"configurable": {"thread_id": session_id}}
                )
                yield {
                    "type": SSEEventType.DONE,
                    "final_response": final_state.values.get("final_response", ""),
                    "analysis_reports": final_state.values.get("analysis_reports", {}),
                }
                break
            if isinstance(event, dict) and "__error__" in event:
                yield {"type": SSEEventType.ERROR, "message": event["__error__"]}
                break

            node = event.get("metadata", {}).get("langgraph_node")
            if node == "supervisor":
                continue

            sse = map_langgraph_event_to_sse(event, filter_type="text")
            if sse is None:
                continue
            if sse["type"] == SSEEventType.TEXT:
                if not _llm_started:
                    _llm_started = True
                    yield {"type": SSEEventType.LLM_START, "label": "正在生成回复"}
                yield sse
    except Exception as exc:
        yield {"type": SSEEventType.ERROR, "message": str(exc)}


async def _stream_updates(graph, initial_state, session_id):
    """updates 流 — 从共享 Queue 读事件，发射 TOOL_START/END + AGENT_SWITCH + DONE"""
    queue, _ = _ensure_queue(session_id)
    _prev_node: str | None = None

    try:
        while True:
            event = await queue.get()
            if event is None:
                yield {"type": SSEEventType.DONE}
                break
            if isinstance(event, dict) and "__error__" in event:
                yield {"type": SSEEventType.ERROR, "message": event["__error__"]}
                break

            node = event.get("metadata", {}).get("langgraph_node")
            if node and node != _prev_node:
                yield {
                    "type": SSEEventType.AGENT_SWITCH,
                    "from_node": _prev_node,
                    "to_node": node,
                }
                _prev_node = node

            sse = map_langgraph_event_to_sse(event, filter_type="tool")
            if sse is not None:
                yield sse
    except Exception as exc:
        yield {"type": SSEEventType.ERROR, "message": str(exc)}


# ── 路由 ──────────────────────────────────────────────────────────


@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> ChatResponse:
    """对话消息（非流式）

    @deprecated: use POST /chat/stream/messages instead.
    保留兼容，前端全部切到双流后清理。
    """
    graph = compile_graph()

    session_id = req.session_id or f"session_{id(req)}"

    initial_state = build_initial_state(
        message=req.message,
        session_id=session_id,
        user_id=req.user_id,
        favorites=req.favorites,
    )

    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
    )

    content = result.get("final_response") or "抱歉，我暂时无法处理您的请求。"
    return ChatResponse(content=content, session_id=session_id)


@router.post("/chat/stream/messages")
async def chat_stream_messages(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> EventSourceResponse:
    """对话消息（SSE 流式，仅文本）— 逐 token 打字

    与 ``/chat/stream/updates`` 共享同一次 graph 执行（asyncio.Queue 扇出）。
    yields: llm_start → text/text/... → done（带 final_response + analysis_reports）
    """
    graph = compile_graph()

    session_id = req.session_id or f"session_{id(req)}"
    initial_state = build_initial_state(
        message=req.message,
        session_id=session_id,
        user_id=req.user_id,
        favorites=req.favorites,
    )

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        async for sse_event in _stream_messages(graph, initial_state, session_id):
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}

    return EventSourceResponse(generator())


@router.post("/chat/stream/updates")
async def chat_stream_updates(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> EventSourceResponse:
    """对话进度（SSE 流式，仅工具事件）— 侧边栏/状态栏

    与 ``/chat/stream/messages`` 共享同一次 graph 执行（asyncio.Queue 扇出）。
    yields: agent_switch → tool_start/tool_end/... → done
    """
    graph = compile_graph()

    session_id = req.session_id or f"session_{id(req)}"
    initial_state = build_initial_state(
        message=req.message,
        session_id=session_id,
        user_id=req.user_id,
        favorites=req.favorites,
    )

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        async for sse_event in _stream_updates(graph, initial_state, session_id):
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}

    return EventSourceResponse(generator())


@router.get("/briefing/morning")
async def morning_briefing() -> EventSourceResponse:
    """晨报（SSE 流式，走 graph 转发）

    复用 ``_stream_messages`` generator，不再调用 morning.stream()。
    缓存命中时 supervisor 花 ~0.5s 分类后立即返回；缓存未命中时逐 token 流式。
    """
    graph = compile_graph()
    session_id = "briefing_morning"

    initial_state = build_initial_state(
        message="生成今日晨报",
        session_id=session_id,
        user_id=None,
        favorites=[],
    )

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        async for sse_event in _stream_messages(graph, initial_state, session_id):
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}

    return EventSourceResponse(generator())
```

Note: Existing routes for `/skills`, `/health`, `/health/ready` remain unchanged below the streaming section. The `HttpClientPool`, `RedisPool`, `settings` imports are kept — they are needed by skills and health routes.

- [ ] **Step 2: Verify routes.py with ruff + mypy**

Run: `ruff check src/aistock_agent/api/routes.py`
Expected: PASS

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/api/routes.py`
Expected: PASS

- [ ] **Step 3: Verify graph compilation**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -c "from aistock_agent.api.routes import compile_graph; g = compile_graph(); print('OK, nodes:', len(g.nodes))"`
Expected: `OK, nodes: 10`

- [ ] **Step 4: Write dual-stream integration tests**

Create new file `tests/integration/test_dual_stream.py`:

```python
"""双流 SSE 端点集成测试"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistock_agent.constants import SSEEventType


# ── 测试 Queue 扇出：单次 graph 执行，两条流各取所需 ──


@pytest.mark.asyncio
async def test_queue_fanout_single_graph_execution():
    """messages 和 updates 共享同一次 graph 执行，不会重复调用 astream_events。"""
    from aistock_agent.api import routes as routes_mod

    call_count = 0

    async def mock_astream_events(initial_state, **kw):
        nonlocal call_count
        call_count += 1
        # 模拟一组事件：supervisor 节点 → stock 节点 → tool 调用 → text 输出
        yield {"event": "on_tool_start", "name": "get_quote",
               "data": {"input": {"symbol": "600519"}},
               "metadata": {"langgraph_node": "stock_analyst"}}
        yield {"event": "on_tool_end", "name": "get_quote",
               "data": {},
               "metadata": {"langgraph_node": "stock_analyst"}}

        chunk = MagicMock()
        chunk.content = "茅台当前价格"
        chunk.tool_calls = []
        chunk.tool_call_chunks = []
        yield {"event": "on_chat_model_stream", "name": "llm",
               "data": {"chunk": chunk},
               "metadata": {"langgraph_node": "stock_analyst"}}

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    # mock aget_state 返回 final
    mock_final = MagicMock()
    mock_final.values = {"final_response": "茅台分析完成", "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)

    session_id = "test-fanout"
    initial_state = {"messages": [], "session_id": session_id}

    # 清理队列
    routes_mod._event_queues.pop(session_id, None)

    # 消费两条流的所有事件
    msg_events = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]
    upd_events = [e async for e in routes_mod._stream_updates(mock_graph, initial_state, session_id)]

    # graph 只执行了一次
    assert call_count == 1

    # messages 流：不含 tool 事件，含 text + done
    msg_types = [e["type"] for e in msg_events]
    assert SSEEventType.TOOL_START not in msg_types
    assert SSEEventType.TOOL_END not in msg_types
    assert SSEEventType.TEXT in msg_types
    assert SSEEventType.DONE in msg_types

    # done 事件携带 final_response
    done_event = msg_events[-1]
    assert done_event["type"] == SSEEventType.DONE
    assert done_event["final_response"] == "茅台分析完成"

    # updates 流：不含 text 事件，含 tool + agent_switch + done
    upd_types = [e["type"] for e in upd_events]
    assert SSEEventType.TEXT not in upd_types
    assert SSEEventType.TOOL_START in upd_types
    assert SSEEventType.TOOL_END in upd_types
    assert SSEEventType.AGENT_SWITCH in upd_types
    assert SSEEventType.DONE in upd_types

    # 清理
    routes_mod._event_queues.pop(session_id, None)


@pytest.mark.asyncio
async def test_queue_fanout_error_propagation():
    """graph 执行异常时，两条流都收到 error 事件。"""
    from aistock_agent.api import routes as routes_mod

    async def mock_astream_events_error(initial_state, **kw):
        raise RuntimeError("graph failure")
        yield  # 使函数成为 async generator（实际不会执行到这里）

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events_error

    session_id = "test-error-fanout"
    initial_state = {"messages": [], "session_id": session_id}

    routes_mod._event_queues.pop(session_id, None)

    msg_events = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]
    upd_events = [e async for e in routes_mod._stream_updates(mock_graph, initial_state, session_id)]

    assert msg_events[0]["type"] == SSEEventType.ERROR
    assert "graph failure" in msg_events[0]["message"]
    assert upd_events[0]["type"] == SSEEventType.ERROR

    routes_mod._event_queues.pop(session_id, None)


@pytest.mark.asyncio
async def test_queue_cleanup_after_stream_complete():
    """流结束后 session queue 被清理，不泄漏内存。"""
    from aistock_agent.api import routes as routes_mod

    async def mock_astream_events(initial_state, **kw):
        chunk = MagicMock()
        chunk.content = "test"
        chunk.tool_calls = []
        chunk.tool_call_chunks = []
        yield {"event": "on_chat_model_stream", "name": "llm",
               "data": {"chunk": chunk},
               "metadata": {"langgraph_node": "general_agent"}}

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events
    mock_final = MagicMock()
    mock_final.values = {"final_response": "test", "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)

    session_id = "test-cleanup"
    initial_state = {"messages": [], "session_id": session_id}

    routes_mod._event_queues.pop(session_id, None)

    _ = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]

    # queue 已被 _run_graph_to_queue 的 finally 块清理
    assert session_id not in routes_mod._event_queues


@pytest.mark.asyncio
async def test_messages_stream_no_updates_connection():
    """仅 messages 连接时（无 updates 连接），正常工作。"""
    from aistock_agent.api import routes as routes_mod

    async def mock_astream_events(initial_state, **kw):
        chunk = MagicMock()
        chunk.content = "独立消息流测试"
        chunk.tool_calls = []
        chunk.tool_call_chunks = []
        yield {"event": "on_chat_model_stream", "name": "llm",
               "data": {"chunk": chunk},
               "metadata": {"langgraph_node": "general_agent"}}

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events
    mock_final = MagicMock()
    mock_final.values = {"final_response": "独立消息流测试", "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)

    session_id = "test-solo-messages"
    initial_state = {"messages": [], "session_id": session_id}

    routes_mod._event_queues.pop(session_id, None)

    events = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]

    text_events = [e for e in events if e["type"] == SSEEventType.TEXT]
    assert len(text_events) == 1
    assert text_events[0]["content"] == "独立消息流测试"
    assert events[-1]["type"] == SSEEventType.DONE

    routes_mod._event_queues.pop(session_id, None)
```

- [ ] **Step 5: Run dual-stream tests**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/integration/test_dual_stream.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Run full test suite**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/ -v 2>&1`
Expected: Same results as Task 3 Step 6 — 330+ pass, 3 pre-existing failures

- [ ] **Step 7: Cleanup — remove old /chat/stream route if it was in the file**

The old `/chat/stream` endpoint (lines 53-111 in current routes.py) is being replaced entirely. Ensure no stale code remains.

- [ ] **Step 8: Commit**

```powershell
git add src/aistock_agent/api/routes.py tests/integration/test_dual_stream.py
git commit -m "feat(api): add dual-stream SSE endpoints (/chat/stream/messages + /chat/stream/updates) with Queue fan-out; refactor /briefing/morning to graph forwarding; deprecate /chat/message"
```

---

### Task 5: Verification — full CI gate

**Files:**
- All modified files from Tasks 1-4

- [ ] **Step 1: ruff check on all modified files**

Run: `ruff check src/aistock_agent/constants.py src/aistock_agent/utils/sse.py src/aistock_agent/agents/workers/morning.py src/aistock_agent/api/routes.py`
Expected: All PASS

- [ ] **Step 2: mypy on all modified files**

Run: `.venv\Scripts\python.exe -m mypy src/aistock_agent/constants.py src/aistock_agent/utils/sse.py src/aistock_agent/agents/workers/morning.py src/aistock_agent/api/routes.py`
Expected: All PASS

- [ ] **Step 3: Full test suite**

Run: `$env:PYTHONPATH = "src"; .venv\Scripts\python.exe -m pytest tests/ -v 2>&1`
Expected: 330+ pass, 3 pre-existing failures (test_log_level_exists, test_intent_set_contents, test_sector_agent_tools_bound_correctly). No NEW failures.

- [ ] **Step 4: Verify graph compiles with new endpoints**

Run:
```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -c "
from aistock_agent.graph.builder import compile_graph
g = compile_graph()
nodes = list(g.nodes.keys()) if hasattr(g, 'nodes') else []
print('Graph nodes:', len(nodes) if nodes else 'unknown (compiled graph does not expose nodes directly)')
print('Compile OK')
"
```
Expected: `Compile OK`

- [ ] **Step 5: Final commit (if changes made during verification)**

```powershell
git status
# If any changes detected, commit them:
git add -A
git commit -m "chore: final CI gate — ruff/mypy/pytest all pass for dual-stream refactor"
```

---

## Task Dependency Graph

```
Task 1 (constants) ──┐
                      ├──► Task 4 (routes) ──► Task 5 (verification)
Task 2 (sse filter) ──┘
                      
Task 3 (morning delete stream) ──► Task 5 (verification)
```

Task 1 and 2 can run in parallel. Task 3 is independent. Task 4 depends on 1+2. Task 5 gates everything.
