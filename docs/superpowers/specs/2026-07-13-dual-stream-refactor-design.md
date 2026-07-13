# Design: 双流 SSE 重构 — 用户对话全流式 + morning stream 合并

**日期**: 2026-07-13
**状态**: Approved
**仓库**: `aistock-agent-py`

---

## 1. 动机

### 1.1 当前问题

前端所有 chat 对话当前走非流式 `POST /chat/message`，用户需要等待 30-60s 才能看到完整结果，体验差。晨报的 `stream()` 和 `run()` 存在功能重复（两者都做 cache check → create agent → 生成 → set cache）。

### 1.2 目标

- 用户对话场景（chat）实现逐 token 流式打字 + 工具进度实时推送（双流架构）
- 合并 morning 的 `stream()` 和 `run()`，消掉重复代码
- **不改动 event / review / iterate / stock / sector agent 代码**，graph 框架自动提供流式能力
- `/chat/message` 标记废弃，保留兼容

---

## 2. 方案选择

选择方案 B：双流分离（messages + updates），相同 graph 连接，路由层分流。

| 方案 | 改动量 | 风险 |
|------|--------|------|
| A: 单流 SSR | 最小 | 低 |
| **B: 双流分离 ✅** | 小 (~5 文件, +200/−150 行) | 低 |
| C: Agent 级 stream + 独立端点 | 大 | 中高 |

**选 B 的理由**: 80% 体验提升来自 20% 改动；所有 agent 已挂在 graph 中，`graph.astream_events(v2)` 自动捕获嵌套 LLM 流事件，无需 agent 级改动。

---

## 3. 架构

```
                          ┌── /chat/stream/messages ──► 主对话气泡
                          │       (TEXT + LLM_START + DONE with final_response)
                          │
graph.astream_events(v2) ─┼── /chat/stream/updates  ──► 侧边栏/状态栏
                          │       (TOOL_START/END + AGENT_SWITCH + DONE)
                          │
                          ├── /briefing/morning     ──► 【合并后走 graph】
                          │       (复用 _stream_messages generator factory)
                          │
                          └── /chat/message         ──► 【标记 @deprecated】
```

### 3.1 双流 = 两个独立 EventSource，共享单次 graph 执行

```
asyncio.create_task(_run_graph())  ←── graph 只执行一次
                │
        asyncio.Queue (扇出)
         ┌──────┴──────┐
         ▼              ▼
  _stream_messages  _stream_updates
         │              │
    EventSource     EventSource
    → 前端气泡      → 前端侧边栏
```

**关键设计**：`graph.astream_events()` 是执行者而非观察者——每次调用都会独立跑一遍 graph。因此不能两个 generator 各自调用，必须**单次执行 + asyncio.Queue 扇出**。

```
POST /chat/stream/messages  → 触发 _run_graph() + 从共享 Queue 读
POST /chat/stream/updates   → 从同一个共享 Queue 读
```

Queue 的 key 为 `session_id`，保证同一对话的 messages 和 updates 流读到同一份事件。

### 3.2 时序

```
时间线 →

messages:  [llm_start]────[text][text]────────────[text]──[done + final]
updates:   [agent_switch]──[tool_start]──[tool_end]──[done]
```

两条连接完全独立，前端可以关闭 updates 流而不影响对话流。

---

## 4. SSE 协议契约

### 4.1 messages 流

```json
{"type": "llm_start", "label": "正在生成回复"}
{"type": "text", "content": "根据"}
{"type": "text", "content": "最新"}
...
{"type": "done", "final_response": "茅台近期受消费复苏...", "analysis_reports": {...}}
```

### 4.2 updates 流

```json
{"type": "agent_switch", "from_node": "supervisor", "to_node": "stock_analyst"}
{"type": "tool_start", "tool": "get_quote", "label": "正在查询个股行情", "args": {"symbol": "600519"}}
{"type": "tool_end", "tool": "get_quote"}
{"type": "agent_switch", "from_node": "stock_analyst", "to_node": "event_analyst"}
{"type": "done"}
```

### 4.3 新增 SSE 事件类型

| type | payload | 用途 |
|------|---------|------|
| `agent_switch` | `{"from_node": "...", "to_node": "..."}` | 子 Agent 切换，侧边栏展示当前执行阶段 |
| `intermediate` | `{"data": {...}}` | 预留 — 结构化中间结果 |

### 4.4 done 事件增强（重排保证）

`done` 不再空 payload，携带 agent 后处理结果：

```json
{
  "type": "done",
  "final_response": "...",           // agent.run() 产出的完整 final_response
  "analysis_reports": {...},         // 结构化数据（display_report, major_events 等）
}
```

**重排流程**：
1. Phase 1: 前端逐 token 渲染 LLM 原始输出
2. Phase 2: graph 跑完后从 `graph.get_state()` 取 `final_response`，用后处理版本替换 raw 流输出
3. event agent 的 JSON 块 → 后处理后的 `podcast_brief`，用户最终看到的是结构化后的播报文本而非 JSON 字符串

---

## 5. 核心实现

### 5.1 共享 Queue 扇出

```python
from asyncio import Queue, create_task

# session_id → asyncio.Queue 映射，保证同一对话的 messages/updates 共享事件流
_event_queues: dict[str, Queue] = {}

def _get_or_create_queue(session_id: str) -> Queue:
    if session_id not in _event_queues:
        _event_queues[session_id] = Queue()
    return _event_queues[session_id]

async def _run_graph(graph, initial_state, session_id):
    """后台执行 graph，事件推入共享 Queue。仅第一次调用时启动。"""
    queue = _get_or_create_queue(session_id)
    try:
        async for event in graph.astream_events(
            initial_state, version="v2",
            config={"configurable": {"thread_id": session_id}},
        ):
            await queue.put(event)
    except Exception as e:
        await queue.put({"__error__": str(e)})
    finally:
        await queue.put(None)           # 哨兵：事件流结束
        _event_queues.pop(session_id, None)  # 清理

async def _stream_messages(graph, initial_state, session_id):
    """messages 流 — 从共享 Queue 读，发射 TEXT + LLM_START + DONE"""
    queue = _get_or_create_queue(session_id)
    _llm_started = False

    # 首次 messages 连接触发 graph 执行，updates 连接只读
    should_start = session_id not in _event_queues
    if should_start:
        _event_queues[session_id] = queue = Queue()
        asyncio.create_task(_run_graph(graph, initial_state, session_id))

    try:
        while True:
            event = await queue.get()
            if event is None:
                # graph 结束，取后处理结果
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
    except Exception:
        yield {"type": SSEEventType.ERROR, "message": str(e)}

async def _stream_updates(graph, initial_state, session_id):
    """updates 流 — 从共享 Queue 读，发射 TOOL_START/END + AGENT_SWITCH + DONE"""
    queue = _get_or_create_queue(session_id)
    _prev_node = None

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
    except Exception:
        yield {"type": SSEEventType.ERROR, "message": str(e)}
```

### 5.3 `map_langgraph_event_to_sse` 改动

加 `filter_type` 参数，最小侵入：

```python
def map_langgraph_event_to_sse(
    event: Mapping[str, Any],
    filter_type: str = "all",   # "all" | "text" | "tool"
) -> dict[str, object] | None:
```

- `"text"` 模式：跳过 `on_tool_start` / `on_tool_end`
- `"tool"` 模式：跳过 `on_chat_model_stream`
- `"all"`：原行为不变

---

## 6. `/briefing/morning` 合并

晨报端点从独立 `morning.stream()` 转为 graph 转发：

```python
# 之前
@router.get("/briefing/morning")
async def morning_briefing():
    async for event in morning_agent.stream(state):
        yield event

# 之后
@router.get("/briefing/morning")
async def morning_briefing():
    graph = compile_graph()
    state = build_initial_state(message="生成今日晨报", session_id="briefing_morning")
    return EventSourceResponse(_stream_messages(graph, state, "briefing_morning"))
```

### 代价分析

缓存命中时多了 0.5s supervisor LLM 分类 → 总计 <1s，用户感知不强。缓存未命中时体验大幅提升（原来非流式 → 现在逐 token）。

---

## 7. morning.py 改动

### 删

- `stream()` 函数（~60 行）
- 相关导入：`AsyncGenerator`, `SSEEventType`, `map_langgraph_event_to_sse`

### 增

- `run()` 内加 `is_trading_day()` 非交易日判断（从 `stream()` 迁入）

### result

```python
async def run(state: AgentState) -> dict[str, object]:
    """晨报分析：cache → create_react_agent → ainvoke → extract → cache + archive"""
    try:
        today = datetime.now().strftime("%Y年%m月%d日")
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

        system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
        if not is_trading_day():
            system_prompt += "\n\n注意：今日为非交易日..."

        llm = get_deep_think()
        tools = get_tools("morning")
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]}
        )
        final_response = extract_final_ai_response(result.get("messages", []))
        major_events = extract_major_events(final_response)

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
        logger.error("agent_run_failed", agent="morning", error=str(e))
        return {"final_response": "晨报生成暂时不可用，请稍后重试"}
```

---

## 8. `/chat/message` 处理

```python
@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(...) -> ChatResponse:
    """对话消息（非流式）

    @deprecated: use POST /chat/stream/messages instead.
    保留兼容，前端全部切到双流后清理。
    """
    ...
```

不删路由，代码不动。仅 docstring 标记废弃。

---

## 9. 路由总览

| 端点 | 状态 | 用途 |
|------|------|------|
| `POST /chat/stream/messages` | **新增** | 主对话气泡流式 |
| `POST /chat/stream/updates` | **新增** | 侧边栏工具进度流式 |
| `GET /briefing/morning` | **重构** | 晨报 SSE（改走 graph 转发） |
| `POST /chat/message` | **废弃（保留）** | 旧非流式兼容端点 |

---

## 10. 文件改动清单

| 文件 | 操作 | 行数变化 |
|------|------|----------|
| `api/routes.py` | 重构：拆 stream 为双流，`/briefing/morning` 改 graph 转发 | +80 / −40 |
| `utils/sse.py` | `map_langgraph_event_to_sse` 加 `filter_type` 参数 | +8 |
| `constants.py` | 新增 `AGENT_SWITCH`, `INTERMEDIATE` 事件类型 | +2 |
| `agents/workers/morning.py` | 删 `stream()`，`run()` 加非交易日判断 | −60 / +5 |
| `tests/` | 新增双流端点集成测试，更新 morning 测试 | +120 / −40 |

### 不动的文件

- `agents/workers/event.py` — graph 层提供流式
- `agents/workers/review.py` — graph 层提供流式
- `agents/workers/stock.py` — graph 层提供流式
- `agents/workers/sector.py` — graph 层提供流式
- `agents/workers/iterate.py` — 不在 graph 中，非用户对话场景
- `graph/builder.py` — 无变化
- `services/cache.py` — 无变化
- `services/scheduler.py` — 无变化

---

## 11. 回退方案

如需回退：
1. 恢复 `morning.py` 的 `stream()` 函数
2. 恢复 `/chat/stream`（单流端点）
3. 删除新增双流端点

所有改动集中在 `routes.py` + `morning.py` + `sse.py`，不涉及 agent 业务逻辑层。
