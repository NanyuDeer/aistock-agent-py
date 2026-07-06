# Phase 3: Morning Agent SSE 流式接口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `/briefing/morning` 端点升级为 SSE 流式响应，补齐非交易日软提示，保持 Redis 缓存机制。

**Architecture:** `routes.py` 的 `/briefing/morning` 直接调用 `morning_agent.stream()`，跳过 StateGraph 和 supervisor；`stream()` 对 `create_react_agent` 返回的内层 `CompiledGraph` 调用 `astream_events(version="v2")`，将 LangGraph 原生事件映射为前端约定的 SSE 格式；Redis 缓存逻辑保持在 `morning_agent.py` 内部。

**Tech Stack:** LangGraph `astream_events` v2、sse-starlette==2.2.1、chinese-calendar==1.10.0、redis.asyncio、pytest-asyncio==0.25.3、httpx==0.28.1

## Global Constraints

- Python ≥ 3.11（使用 `date | None` 联合类型语法）
- `chinese-calendar==1.10.0`（固定版本，不升级）
- SSE 事件字段：`type` 取值为 `tool_start` `tool_end` `llm_start` `text` `done` `error`
- Redis 异常静默处理，不中断流
- LLM 调用失败 yield `{"type":"error","message":"..."}` 后 return
- 所有测试 mock，不依赖真实网络或 Redis

---

### Task 1: 添加依赖 + 移动 Redis 导入 + `is_trading_day()`

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/aistock_agent/agents/morning_agent.py`（顶部导入段）
- Create: `tests/test_morning_agent.py`

**Interfaces:**
- Produces: `is_trading_day(d: date | None = None) -> bool`（Task 2 引用）
- Produces: 模块级 `import redis.asyncio as aioredis`（`conftest.mock_redis` 依赖该路径）

- [ ] **Step 1: 在 `pyproject.toml` 的 dependencies 中添加 `chinese-calendar`**

  在 `"yfinance==0.2.54",` 行后新增一行：

  ```toml
  "chinese-calendar==1.10.0", # A 股交易日判断
  ```

- [ ] **Step 2: 安装新依赖**

  ```bash
  pip install -e ".[dev]"
  ```

  预期输出含：`Successfully installed chinese-calendar-1.10.0`

- [ ] **Step 3: 创建 `tests/test_morning_agent.py` 并写 `is_trading_day` 失败测试**

  ```python
  """morning_agent 测试"""
  import pytest
  from datetime import date

  from aistock_agent.agents.morning_agent import is_trading_day


  def test_is_trading_day_weekday():
      # 2026-07-06 是周一
      assert is_trading_day(date(2026, 7, 6)) is True


  def test_is_trading_day_saturday():
      # 2026-07-04 是周六
      assert is_trading_day(date(2026, 7, 4)) is False


  def test_is_trading_day_national_holiday():
      # 2026-10-01 是国庆节
      assert is_trading_day(date(2026, 10, 1)) is False


  def test_is_trading_day_no_arg_returns_bool():
      # 不传参数时调用 date.today()，验证不崩溃且返回 bool
      result = is_trading_day()
      assert isinstance(result, bool)
  ```

- [ ] **Step 4: 运行测试确认失败（`is_trading_day` 尚未存在）**

  ```bash
  pytest tests/test_morning_agent.py -v
  ```

  预期：4 个测试 FAIL（`ImportError: cannot import name 'is_trading_day'`）

- [ ] **Step 5: 将 `morning_agent.py` 的 redis 导入移至模块顶层，并添加新依赖导入**

  当前文件顶部只有：
  ```python
  import json
  from datetime import datetime
  ```

  改为：
  ```python
  import json
  from collections.abc import AsyncGenerator
  from datetime import date, datetime

  import redis.asyncio as aioredis
  from chinese_calendar import is_workday
  ```

  同时删除 `_get_cached_briefing` 和 `_set_cached_briefing` 函数体内的
  `import redis.asyncio as aioredis` 行（各函数内各一行，共删两行）。

- [ ] **Step 6: 在 `morning_agent.py` 末尾添加 `is_trading_day()`**

  ```python
  def is_trading_day(d: date | None = None) -> bool:
      """判断是否为 A 股交易日（排除周末和法定节假日）"""
      return is_workday(d or date.today())
  ```

- [ ] **Step 7: 运行测试确认全部通过**

  ```bash
  pytest tests/test_morning_agent.py -v
  ```

  预期：4 个测试全部 PASS

- [ ] **Step 8: Commit**

  ```bash
  git add pyproject.toml src/aistock_agent/agents/morning_agent.py tests/test_morning_agent.py
  git commit -m "feat(phase3): add chinese-calendar dep and is_trading_day() helper"
  ```

---

### Task 2: `morning_agent.TOOL_LABELS` + `stream()` 异步生成器

**Files:**
- Modify: `src/aistock_agent/agents/morning_agent.py`
- Modify: `tests/test_morning_agent.py`

**Interfaces:**
- Consumes: `is_trading_day()` from Task 1
- Produces: `TOOL_LABELS: dict[str, str]`（模块级常量）
- Produces: `async def stream(state: dict) -> AsyncGenerator[dict, None]`（Task 3 调用）

- [ ] **Step 1: 在 `tests/test_morning_agent.py` 末尾追加 stream 失败测试**

  ```python
  # ── stream() 测试 ──────────────────────────────────────────────────
  from unittest.mock import AsyncMock, MagicMock
  from aistock_agent.agents import morning_agent


  async def _async_iter(items):
      """将列表转换为异步迭代器，用于 mock astream_events"""
      for item in items:
          yield item


  @pytest.mark.asyncio
  async def test_stream_cache_hit(mock_redis):
      """缓存命中：只 yield text + done，不调用 LLM"""
      mock_redis.get.return_value = b"cached briefing content"

      events = [e async for e in morning_agent.stream({})]

      assert events == [
          {"type": "text", "content": "cached briefing content"},
          {"type": "done"},
      ]


  @pytest.mark.asyncio
  async def test_stream_tool_events_mapped(mock_redis):
      """tool_start/tool_end 正确映射标签，tavily 带 args"""
      mock_redis.get.return_value = None
      mock_redis.setex = AsyncMock()

      raw_events = [
          {"event": "on_tool_start", "name": "get_global_markets",
           "data": {"input": {}}},
          {"event": "on_tool_end", "name": "get_global_markets", "data": {}},
          {"event": "on_tool_start", "name": "tavily_finance_search",
           "data": {"input": {"query": "美联储利率"}}},
          {"event": "on_tool_end", "name": "tavily_finance_search", "data": {}},
      ]

      mock_agent = MagicMock()
      mock_agent.astream_events = lambda *a, **kw: _async_iter(raw_events)

      with patch("aistock_agent.agents.morning_agent.create_react_agent",
                 return_value=mock_agent):
          with patch("aistock_agent.agents.morning_agent.is_trading_day",
                     return_value=True):
              events = [e async for e in morning_agent.stream({})]

      assert {"type": "tool_start", "tool": "get_global_markets",
              "label": "正在获取全球市场行情"} in events
      assert {"type": "tool_end", "tool": "get_global_markets"} in events
      assert {"type": "tool_start", "tool": "tavily_finance_search",
              "label": "正在搜索财经新闻",
              "args": {"query": "美联储利率"}} in events
      assert events[-1] == {"type": "done"}


  @pytest.mark.asyncio
  async def test_stream_filters_tool_call_chunks(mock_redis):
      """带 tool_calls 的 chunk 不产生 text 事件，纯文本 chunk 正常 yield"""
      mock_redis.get.return_value = None
      mock_redis.setex = AsyncMock()

      tool_chunk = MagicMock()
      tool_chunk.content = "thinking..."
      tool_chunk.tool_calls = [{"name": "get_global_markets"}]
      tool_chunk.tool_call_chunks = []

      text_chunk = MagicMock()
      text_chunk.content = "今日市场分析"
      text_chunk.tool_calls = []
      text_chunk.tool_call_chunks = []

      raw_events = [
          {"event": "on_chat_model_stream", "name": "llm",
           "data": {"chunk": tool_chunk}},
          {"event": "on_chat_model_stream", "name": "llm",
           "data": {"chunk": text_chunk}},
      ]

      mock_agent = MagicMock()
      mock_agent.astream_events = lambda *a, **kw: _async_iter(raw_events)

      with patch("aistock_agent.agents.morning_agent.create_react_agent",
                 return_value=mock_agent):
          with patch("aistock_agent.agents.morning_agent.is_trading_day",
                     return_value=True):
              events = [e async for e in morning_agent.stream({})]

      text_events = [e for e in events if e.get("type") == "text"]
      assert len(text_events) == 1
      assert text_events[0]["content"] == "今日市场分析"
      assert {"type": "llm_start", "label": "正在生成分析报告"} in events


  @pytest.mark.asyncio
  async def test_stream_non_trading_day_injects_prompt(mock_redis):
      """非交易日时 system prompt 包含非交易日提示"""
      mock_redis.get.return_value = None
      mock_redis.setex = AsyncMock()
      captured: dict = {}

      def fake_create(llm, tools):
          mock_inner = MagicMock()

          async def fake_astream(inp, **kw):
              captured.update(inp)
              return
              yield  # 使其成为 async generator

          mock_inner.astream_events = fake_astream
          return mock_inner

      with patch("aistock_agent.agents.morning_agent.create_react_agent",
                 side_effect=fake_create):
          with patch("aistock_agent.agents.morning_agent.is_trading_day",
                     return_value=False):
              _ = [e async for e in morning_agent.stream({})]

      messages = captured.get("messages", [])
      assert messages, "messages should not be empty"
      assert "非交易日" in messages[0].content
  ```

- [ ] **Step 2: 运行失败测试**

  ```bash
  pytest tests/test_morning_agent.py -k "stream" -v
  ```

  预期：所有 stream 测试 FAIL（`AttributeError: module 'morning_agent' has no attribute 'stream'`）

- [ ] **Step 3: 在 `morning_agent.py` 的 `run()` 函数前添加 `TOOL_LABELS` 和 `stream()`**

  ```python
  TOOL_LABELS: dict[str, str] = {
      "get_global_markets":    "正在获取全球市场行情",
      "tavily_finance_search": "正在搜索财经新闻",
      "get_cls_news":          "正在获取财联社资讯",
  }


  async def stream(state: dict) -> AsyncGenerator[dict, None]:
      """晨报 SSE 流：缓存命中直接返回，未命中走 ReAct + astream_events"""
      today = datetime.now().strftime("%Y年%m月%d日")

      cached = await _get_cached_briefing()
      if cached:
          yield {"type": "text", "content": cached}
          yield {"type": "done"}
          return

      system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
      if not is_trading_day():
          system_prompt += (
              "\n\n注意：今日为非交易日（周末或节假日），"
              "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
          )

      llm = get_deep_think()
      tools = [tavily_finance_search, get_global_markets, get_cls_news]
      react_agent = create_react_agent(llm, tools)

      _llm_started = False
      _response_parts: list[str] = []

      try:
          async for event in react_agent.astream_events(
              {"messages": [SystemMessage(content=system_prompt)]},
              version="v2",
          ):
              event_type = event.get("event")
              tool_name = event.get("name", "")

              if event_type == "on_tool_start":
                  label = TOOL_LABELS.get(tool_name, tool_name)
                  tool_event: dict = {
                      "type": "tool_start",
                      "tool": tool_name,
                      "label": label,
                  }
                  query = event.get("data", {}).get("input", {}).get("query")
                  if query:
                      tool_event["args"] = {"query": query}
                  yield tool_event

              elif event_type == "on_tool_end":
                  yield {"type": "tool_end", "tool": tool_name}

              elif event_type == "on_chat_model_stream":
                  chunk = event.get("data", {}).get("chunk")
                  if chunk:
                      has_text = bool(chunk.content)
                      has_tool_calls = bool(
                          getattr(chunk, "tool_calls", None)
                          or getattr(chunk, "tool_call_chunks", None)
                      )
                      if has_text and not has_tool_calls:
                          if not _llm_started:
                              _llm_started = True
                              yield {"type": "llm_start", "label": "正在生成分析报告"}
                          yield {"type": "text", "content": chunk.content}
                          _response_parts.append(chunk.content)

          final_response = "".join(_response_parts)
          if final_response:
              await _set_cached_briefing(final_response)

      except Exception as e:
          yield {"type": "error", "message": str(e)}
          return

      yield {"type": "done"}
  ```

- [ ] **Step 4: 运行 stream 测试确认通过**

  ```bash
  pytest tests/test_morning_agent.py -v
  ```

  预期：全部 PASS

- [ ] **Step 5: 运行 ruff 检查**

  ```bash
  ruff check src/aistock_agent/agents/morning_agent.py
  ```

  预期：无报错

- [ ] **Step 6: Commit**

  ```bash
  git add src/aistock_agent/agents/morning_agent.py tests/test_morning_agent.py
  git commit -m "feat(phase3): add morning_agent.stream() with SSE event mapping"
  ```

---

### Task 3: `routes.py` SSE 端点替换 + 路由测试

**Files:**
- Modify: `src/aistock_agent/api/routes.py`
- Create: `tests/test_routes_briefing.py`

**Interfaces:**
- Consumes: `morning_agent.stream(state: dict) -> AsyncGenerator[dict, None]` from Task 2
- Produces: `GET /api/agent/briefing/morning` 返回 `text/event-stream`

- [ ] **Step 1: 创建 `tests/test_routes_briefing.py` 写失败测试**

  ```python
  """routes /briefing/morning SSE 端点测试"""
  import json
  import pytest
  import httpx
  from unittest.mock import patch

  from aistock_agent.main import app


  async def _mock_stream_ok(state):
      yield {"type": "tool_start", "tool": "get_global_markets",
             "label": "正在获取全球市场行情"}
      yield {"type": "tool_end", "tool": "get_global_markets"}
      yield {"type": "llm_start", "label": "正在生成分析报告"}
      yield {"type": "text", "content": "今日晨报内容"}
      yield {"type": "done"}


  async def _mock_stream_error(state):
      yield {"type": "error", "message": "LLM unavailable"}


  @pytest.mark.asyncio
  async def test_briefing_morning_content_type():
      """响应 Content-Type 必须是 text/event-stream"""
      with patch("aistock_agent.api.routes.morning_agent.stream",
                 side_effect=_mock_stream_ok):
          async with httpx.AsyncClient(
              transport=httpx.ASGITransport(app=app),
              base_url="http://test",
          ) as client:
              async with client.stream("GET", "/api/agent/briefing/morning") as resp:
                  assert resp.status_code == 200
                  assert "text/event-stream" in resp.headers["content-type"]


  @pytest.mark.asyncio
  async def test_briefing_morning_sse_events():
      """SSE 数据行可解析为预期 JSON 事件序列"""
      with patch("aistock_agent.api.routes.morning_agent.stream",
                 side_effect=_mock_stream_ok):
          async with httpx.AsyncClient(
              transport=httpx.ASGITransport(app=app),
              base_url="http://test",
          ) as client:
              async with client.stream("GET", "/api/agent/briefing/morning") as resp:
                  data_events = []
                  async for line in resp.aiter_lines():
                      if line.startswith("data:"):
                          data_events.append(json.loads(line[5:].strip()))

          types = [e["type"] for e in data_events]
          assert "tool_start" in types
          assert "text" in types
          assert types[-1] == "done"

          text_event = next(e for e in data_events if e["type"] == "text")
          assert text_event["content"] == "今日晨报内容"


  @pytest.mark.asyncio
  async def test_briefing_morning_error_event():
      """stream 内部 yield error 时，SSE 正确传递 error 事件"""
      with patch("aistock_agent.api.routes.morning_agent.stream",
                 side_effect=_mock_stream_error):
          async with httpx.AsyncClient(
              transport=httpx.ASGITransport(app=app),
              base_url="http://test",
          ) as client:
              async with client.stream("GET", "/api/agent/briefing/morning") as resp:
                  data_events = []
                  async for line in resp.aiter_lines():
                      if line.startswith("data:"):
                          data_events.append(json.loads(line[5:].strip()))

          assert data_events[0] == {"type": "error", "message": "LLM unavailable"}
  ```

- [ ] **Step 2: 运行失败测试**

  ```bash
  pytest tests/test_routes_briefing.py -v
  ```

  预期：全部 FAIL（`/briefing/morning` 当前返回 dict，非 SSE）

- [ ] **Step 3: 修改 `routes.py`——新增导入**

  在文件顶部现有导入后添加：

  ```python
  import json

  from sse_starlette.sse import EventSourceResponse

  from aistock_agent.agents import morning_agent
  ```

  （`Optional` 和 `Header` 等已有导入保持不变）

- [ ] **Step 4: 替换 `routes.py` 中的 `/briefing/morning` 端点**

  删除现有：

  ```python
  @router.get("/briefing/morning")
  async def morning_briefing() -> dict:
      """晨报（非流式，支持 Redis 缓存）"""
      graph = compile_graph()

      initial_state = {
          "messages": [{"role": "user", "content": "生成今日晨报"}],
          "session_id": "briefing_morning",
          "user_id": None,
          "favorites": [],
          "intent": "morning",
          "symbol": None,
          "tag_code": None,
          "analysis_reports": {},
          "final_response": None,
      }

      result = await graph.ainvoke(initial_state)

      content = result.get("final_response") or "晨报生成失败，请稍后重试。"
      return {"content": content}
  ```

  替换为：

  ```python
  @router.get("/briefing/morning")
  async def morning_briefing() -> EventSourceResponse:
      """晨报（SSE 流式，支持 Redis 缓存）"""
      state = {
          "messages": [{"role": "user", "content": "生成今日晨报"}],
          "session_id": "briefing_morning",
          "user_id": None,
          "favorites": [],
          "intent": "morning",
          "symbol": None,
          "tag_code": None,
          "analysis_reports": {},
          "final_response": None,
      }

      async def generator():
          try:
              async for event in morning_agent.stream(state):
                  yield {"data": json.dumps(event, ensure_ascii=False)}
          except Exception as e:
              yield {"data": json.dumps(
                  {"type": "error", "message": str(e)},
                  ensure_ascii=False,
              )}

      return EventSourceResponse(generator())
  ```

- [ ] **Step 5: 运行路由测试确认通过**

  ```bash
  pytest tests/test_routes_briefing.py -v
  ```

  预期：全部 PASS

- [ ] **Step 6: 运行全量测试确认无回归**

  ```bash
  pytest tests/ -v
  ```

  预期：全部 PASS

- [ ] **Step 7: ruff + mypy 检查**

  ```bash
  ruff check src/
  mypy src/
  ```

  预期：无 error（mypy strict 模式下 `AsyncGenerator` 类型已在导入）

- [ ] **Step 8: Commit**

  ```bash
  git add src/aistock_agent/api/routes.py tests/test_routes_briefing.py
  git commit -m "feat(phase3): replace /briefing/morning with SSE EventSourceResponse"
  ```

---

### Task 4: 文档更新

**Files:**
- Modify: `docs/refactor-plan.md`

**Interfaces:**
- 无代码依赖；纯文档操作

- [ ] **Step 1: 在 `docs/refactor-plan.md` 第 12 节勾掉全部 4 条待确认事项**

  将 Section 12 从：

  ```markdown
  - [ ] Python服务端口（Node.js当前端口？）
  - [ ] Redis地址（与Node.js共用还是独立实例？）
  - [ ] Tavily API Key申请
  - [ ] 服务器Python环境（3.11+）
  ```

  改为：

  ```markdown
  - [x] Python 服务端口：8000（已在 .env.example 确认）
  - [x] Redis：与 Node.js 共用，`redis://localhost:6379/1`
  - [x] Tavily API Key：7 Key 轮换池，已在 config.py + .env.example 实现
  - [x] 服务器 Python 环境：3.11
  ```

- [ ] **Step 2: 在 `docs/refactor-plan.md` 第 10 节的阶段表中更新状态**

  将 Phase 表格改为：

  ```markdown
  | Phase | 内容 | 核心产出 | 验收标准 | 状态 |
  |-------|------|----------|----------|------|
  | **1** | 项目骨架 | pyproject.toml / config / AgentState / FastAPI `/health` | `uvicorn`启动，`/health` 返回200 | ✅ 完成 |
  | **2** | Node.js内部API + Python Tools层 | 6个`/internal/*`接口 + 5个`@tool` | 每个tool有pytest，独立可调用 | ✅ 完成 |
  | **3** | Morning Agent | `agents/morning_agent.py` + Redis缓存 + SSE接口 | `/briefing/morning` SSE流式返回4步分析 | 🔄 进行中 |
  | **4** | 对话Agent层 | supervisor + stock/sector/event/general agent + graph builder | 完整消息流程：输入→路由→工具调用→回复 | ⏳ 待开始 |
  | **5** | Node.js接入 + 标准文档 | Express反代 + `AGENT_STANDARDS.md` | 端到端测试通过，文档覆盖所有扩展场景 | ⏳ 待开始 |
  ```

- [ ] **Step 3: 更新文档头部状态字段**

  将第一行：

  ```markdown
  > 版本：v1.0 | 日期：2026-07-04 | 状态：规划确认
  ```

  改为：

  ```markdown
  > 版本：v1.1 | 日期：2026-07-04 | 状态：Phase 3 进行中
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add docs/refactor-plan.md
  git commit -m "docs: update refactor-plan phase progress and close pending items"
  ```

