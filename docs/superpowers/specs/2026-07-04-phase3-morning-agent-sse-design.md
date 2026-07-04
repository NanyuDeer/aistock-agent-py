# Phase 3 设计文档 — Morning Agent SSE 流式接口

> 版本：v1.0 | 日期：2026-07-04 | 状态：设计确认

---

## 1. 背景

Phase 1（项目骨架）和 Phase 2（Tools 层 + Node.js 数据接口）已完成。

Phase 3 目标：将 `/briefing/morning` 接口升级为 SSE 流式响应，补齐非交易日提示，
保持 Redis 缓存机制，确保 Morning Agent 可在生产环境独立使用。

当前实现缺口：
- `routes.py` 的 `/briefing/morning` 使用 `ainvoke` 返回普通 dict，不是 SSE
- `morning_agent.py` 缺少非交易日判断
- `pyproject.toml` 缺少 `chinese-calendar` 依赖

---

## 2. 已确认决策

| 决策项 | 结论 |
|--------|------|
| SSE 事件粒度 | 阶段级（工具进度 + LLM token 流） |
| 非交易日处理 | 软警告：注入提示词，不阻断流程 |
| Python 服务端口 | 8000 |
| Redis | 与 Node.js 共用，`redis://localhost:6379/1` |
| Tavily API | 7 Key 轮换池（已在 config.py + .env.example 实现） |
| 服务器 Python 版本 | 3.11 |

---

## 3. 架构变更范围

`/briefing/morning` 专用端点意图已知，**不需要经过 StateGraph 和 supervisor**。
路由直接调用 `morning_agent.stream()`，内部对 `create_react_agent` 返回的
`CompiledGraph` 调用 `astream_events`，无嵌套透传问题。

```
GET /briefing/morning
  └─ morning_agent.stream(state)
       ├─ 检查 Redis 缓存
       │   命中 → yield text(缓存内容) + done
       └─ 未命中
           ├─ is_trading_day() 检查 → 非交易日注入提示词
           ├─ create_react_agent(llm, tools).astream_events(...)
           │   ├─ on_tool_start  → yield tool_start
           │   ├─ on_tool_end    → yield tool_end
           │   └─ on_chat_model_stream（有文本且无 tool_calls）
           │       首次 → yield llm_start，yield text
           │       后续 → yield text
           └─ 写入 Redis 缓存 → yield done
```

**不变动的模块**：`graph/builder.py`、`graph/edges.py`、`state/schema.py`、
`agents/base.py`、所有 tools、`agents/morning_agent.run()`（保留供图调用）。

---

## 4. SSE 事件格式

所有事件以 `data: <JSON>\n\n` 格式通过 `text/event-stream` 推送。

### 4.1 事件类型

| type | 字段 | 触发时机 |
|------|------|---------|
| `tool_start` | `tool`、`label`、`args`（可选） | `on_tool_start` |
| `tool_end` | `tool` | `on_tool_end` |
| `llm_start` | `label: "正在生成分析报告"` | 第一个有效文本 chunk 前 |
| `text` | `content` | `on_chat_model_stream` 有效 chunk |
| `done` | 无 | 流结束 |
| `error` | `message` | 异常 |

### 4.2 工具标签映射

| tool name | label |
|-----------|-------|
| `get_global_markets` | 正在获取全球市场行情 |
| `tavily_finance_search` | 正在搜索财经新闻 |
| `get_cls_news` | 正在获取财联社资讯 |

`tavily_finance_search` 的 `tool_start` 额外附带 `args: {"query": "<搜索词>"}`，
搜索词从 `event["data"]["input"]["query"]` 取得。

### 4.3 典型事件序列

**缓存命中：**
```
data: {"type":"text","content":"...完整报告..."}
data: {"type":"done"}
```

**缓存未命中：**
```
data: {"type":"tool_start","tool":"get_global_markets","label":"正在获取全球市场行情"}
data: {"type":"tool_end","tool":"get_global_markets"}
data: {"type":"tool_start","tool":"tavily_finance_search","label":"正在搜索财经新闻","args":{"query":"美联储利率决议"}}
data: {"type":"tool_end","tool":"tavily_finance_search"}
data: {"type":"llm_start","label":"正在生成分析报告"}
data: {"type":"text","content":"今日市场概况..."}
data: {"type":"text","content":"（更多 token）"}
data: {"type":"done"}
```

---

## 5. `morning_agent.py` 变更

### 5.1 新增 `is_trading_day()`

```python
from chinese_calendar import is_workday
from datetime import date

def is_trading_day(d: date | None = None) -> bool:
    """判断是否为 A 股交易日（排除周末和法定节假日）"""
    return is_workday(d or date.today())
```

### 5.2 新增 `stream()` 异步生成器

签名：
```python
async def stream(state: dict) -> AsyncGenerator[dict, None]:
```

逻辑：
1. 检查 Redis 缓存（复用 `_get_cached_briefing()`）
2. 缓存命中 → `yield {"type": "text", "content": cached}` + `yield {"type": "done"}`，return
3. 非交易日检查 → 向 `system_prompt` 追加非交易日提示
4. 初始化 `_llm_started = False`、`_response_parts: list[str] = []`
5. `create_react_agent(llm, tools).astream_events(input, version="v2")` 循环
6. 事件过滤（见 5.3）；有效文本 chunk 同时 `_response_parts.append(chunk.content)`
7. 循环结束后，`final_response = "".join(_response_parts)`；非空时写入 Redis
8. `yield {"type": "done"}`

### 5.3 `on_chat_model_stream` 过滤条件

```python
chunk = event["data"]["chunk"]
has_text = bool(chunk.content)
has_tool_calls = bool(
    getattr(chunk, "tool_calls", None) or
    getattr(chunk, "tool_call_chunks", None)
)
if has_text and not has_tool_calls:
    # yield text 事件
```

仅当内容非空且不含 tool_calls 时推送，过滤 ReAct 思考阶段的中间 chunk。

首次命中时将 `_llm_started` 由 `False` 置为 `True`，先 yield `llm_start` 再 yield `text`；
后续命中直接 yield `text`。

### 5.4 `run()` 保持不变

`run(state: AgentState) -> dict` 供 `graph/builder.py` 节点调用，不修改。

---

## 6. `routes.py` 变更

### 6.1 替换 `/briefing/morning`

```python
from sse_starlette.sse import EventSourceResponse
import json

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
            yield {"data": json.dumps({"type": "error", "message": str(e)})}

    return EventSourceResponse(generator())
```

### 6.2 新增 TOOL_LABELS

```python
TOOL_LABELS: dict[str, str] = {
    "get_global_markets":    "正在获取全球市场行情",
    "tavily_finance_search": "正在搜索财经新闻",
    "get_cls_news":          "正在获取财联社资讯",
}
```

---

## 7. `pyproject.toml` 变更

在 `dependencies` 中新增：

```toml
"chinese-calendar==1.10.0",  # A 股交易日判断
```

---

## 8. `docs/refactor-plan.md` 更新

- Section 12（待确认事项）：全部4条标为已解决
- Section 10（实施计划）：Phase 1、Phase 2 标为 ✅，Phase 3 标为进行中

---

## 9. 错误处理

| 场景 | 处理方式 |
|------|---------|
| Redis 连接失败 | 静默跳过缓存，正常走 LLM 流程 |
| yfinance / Tavily 工具调用失败 | Tool 内部已降级返回文本，不抛异常 |
| LLM 调用失败 | `stream()` 捕获异常，yield `error` 事件后结束 |
| 空响应 | `done` 事件仍发送，`final_response` 为空字符串，不写缓存 |

---

## 10. 测试计划

| 测试文件 | 覆盖点 |
|---------|-------|
| `tests/test_morning_agent.py` | `is_trading_day()` 边界（周末/节假日/工作日）；缓存命中返回路径；`stream()` 事件序列（mock react_agent） |
| `tests/test_routes_briefing.py` | SSE 响应 Content-Type；缓存命中时事件格式；error 事件格式 |
