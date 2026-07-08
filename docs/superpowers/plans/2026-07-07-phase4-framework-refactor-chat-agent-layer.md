# Phase 4: 框架物理重构 + 核心对话层完善 + 持久化记忆 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按 Task 推进。Steps 用 checkbox（`- [ ]`）跟踪。

**Goal:** 在不改变现有业务行为的前提下，对 `aistock_agent` 包做目录物理分层重构（agents/services/graph/prompts/utils/schemas/memory/constants/errors），补齐测试覆盖、异常降级、SSE 流式对话接口，并落地 LangGraph checkpointer 持久化记忆，使核心对话流程达到验收标准"完整消息流程：输入→路由→工具调用→回复"且多轮对话可恢复。

**Architecture:** 保持现有"START → supervisor → [条件路由] → workers/general → END"拓扑不变。重构聚焦在物理目录分层与职责归位：模型工厂迁出 `agents/`，路由函数归入 `graph/routers/`，业务 Agent 收敛到 `agents/workers/`，对外 Pydantic 模型独立到 `schemas/`，会话持久化独立到 `memory/`，通用工具收敛到 `utils/`，常量与异常体系单独立项。

**Tech Stack:** LangGraph `astream_events` v2、`sse-starlette==2.2.1`、`langgraph.checkpoint.redis`（或 sqlite）、`pydantic==2.11.5`、`structlog==25.3.0`（仅引入，深度使用在 Phase 5）、`pytest-asyncio==0.25.3`

## Global Constraints

- Python ≥ 3.11（使用 `X | None` 联合类型语法）
- 重构遵循"先读后改、增量迁移"原则，禁止全量重写已通过测试的逻辑
- 重构期间 `tests/test_morning_agent.py` 和 `tests/test_routes_briefing.py` 必须保持全绿（回归基线）
- 每个 Task 完成后必须运行 `ruff check src/ && mypy src/ && pytest tests/ -v` 三件套
- 业务 Agent 的 `run()` 必须有顶层 try-catch，工具/LLM 失败返回降级文本，不抛异常中断图
- SSE 事件类型常量统一来自 `constants.py`，禁止 magic string
- `agents/base.py` 迁移到 `services/llm.py` 后，旧路径保留 re-export 一个版本以兼容外部引用（若存在），下一版删除
- memory/ 的 checkpointer 必须可插拔（开发用 SqliteSaver，生产用 RedisSaver），通过 config 切换
- 所有新模块必须有 `__init__.py`，禁止 namespace package

---

## 重构前后目录对比

### BEFORE（当前结构）

```
src/aistock_agent/
├── main.py
├── config.py
├── state/
│   └── schema.py
├── graph/
│   ├── builder.py
│   └── edges.py              ← 条件边散落在 graph 根
├── agents/
│   ├── __init__.py
│   ├── base.py               ← 模型工厂（错位：属底层服务）
│   ├── supervisor.py         ← 路由决策（与业务 Worker 混放）
│   ├── morning_agent.py      ← 业务 Worker
│   ├── stock_analyst.py      ← 业务 Worker
│   ├── sector_analyst.py     ← 业务 Worker
│   ├── event_analyst.py      ← 业务 Worker
│   └── general_agent.py      ← 兜底（与业务 Worker 混放）
├── tools/
│   ├── stock_tools.py
│   ├── sector_tools.py
│   ├── news_tools.py
│   ├── market_tools.py
│   ├── monitor_tools.py      ← 未实现（Phase 5）
│   └── tenx_tools.py         ← 未实现（Phase 5）
├── prompts/
│   ├── morning.py
│   ├── routing.py
│   └── system.py             ← 所有 Agent 提示词混在一起
├── services/
│   └── data_client.py
└── api/
    ├── routes.py             ← _verify_internal_token 内联、state 构造散落
    └── ws.py                 ← 不存在

tests/                        ← 8 个测试文件全堆根目录，未分层
├── conftest.py
├── test_morning_agent.py
├── test_routes_briefing.py
├── test_stock_tools.py
├── test_sector_tools.py
├── test_news_tools.py
├── test_market_tools.py
└── __init__.py
```

### AFTER（重构后结构）

```
src/aistock_agent/
├── main.py                   ← Phase 5 加 lifespan，本 Phase 不动
├── config.py
├── constants.py              ★ 新增：SSE 事件类型 / intent 集合 / 错误码 / TOOL_LABELS
├── state/
│   └── schema.py
├── schemas/                  ★ 新增：对外交互 Pydantic 数据模型
│   ├── __init__.py
│   ├── chat.py               ← ChatRequest / ChatResponse（从 api/routes.py 抽出）
│   ├── sse.py                ← SSEEvent 统一模型
│   └── agents.py             ← 各 Agent 输入/输出 schema
├── memory/                   ★ 新增：持久化记忆模块
│   ├── __init__.py
│   ├── checkpointer.py       ← LangGraph checkpointer 工厂（Sqlite/Redis 可切换）
│   ├── session_store.py      ← 会话历史读写
│   └── preferences.py        ← 用户偏好/自选股记忆
├── utils/                    ★ 新增：通用工具
│   ├── __init__.py
│   ├── sse.py                ← LangGraph 事件 → SSE 事件映射（从 morning_agent 抽出）
│   ├── parser.py             ← LLM 输出解析（_parse_intent 等）
│   ├── message.py            ← 消息提取（取最后一条 human 消息等）
│   └── date.py               ← 日期/交易日工具（is_trading_day 迁入）
├── errors/                   ★ 新增：异常体系
│   ├── __init__.py
│   └── exceptions.py         ← DataUnavailableError / LLMTimeoutError / ToolExecutionError / RouteError
├── services/                 ★ 增强：全局资源封装
│   ├── __init__.py
│   ├── llm.py                ← 模型工厂（从 agents/base.py 迁移）
│   ├── cache.py              ← Redis 缓存抽象（从 morning_agent 抽出，B5）
│   ├── data_client.py        ← 保留：httpx → Node.js /internal/*
│   └── tavily.py             ← Tavily 客户端封装（从 market_tools 抽出）
├── graph/
│   ├── __init__.py
│   ├── builder.py            ← 保留：StateGraph 构建 + compile()
│   └── routers/              ★ 新增：条件边路由函数集中
│       ├── __init__.py
│       └── intent_router.py  ← route_by_intent（从 edges.py 迁入）
├── agents/
│   ├── __init__.py
│   ├── supervisor/           ★ 新增：调度专属
│   │   ├── __init__.py
│   │   └── node.py           ← 路由决策节点（从 supervisor.py 迁入）
│   ├── general/              ★ 新增：兜底通用
│   │   ├── __init__.py
│   │   └── node.py           ← 兜底节点（从 general_agent.py 迁入）
│   └── workers/              ★ 新增：深度业务专业智能体
│       ├── __init__.py
│       ├── morning.py        ← 从 morning_agent.py 迁入
│       ├── stock.py          ← 从 stock_analyst.py 迁入
│       ├── sector.py         ← 从 sector_analyst.py 迁入
│       └── event.py          ← 从 event_analyst.py 迁入
├── tools/
│   ├── __init__.py
│   ├── base.py               ★ 新增：通用 @tool 基类（错误处理 mixin + 参数校验）
│   ├── stock_tools.py
│   ├── sector_tools.py
│   ├── news_tools.py
│   ├── market_tools.py
│   ├── monitor_tools.py      ← 占位（Phase 5 实现）
│   └── tenx_tools.py         ← 占位（Phase 5 实现）
├── prompts/                  ★ 分层对应 agents 目录
│   ├── __init__.py
│   ├── supervisor/
│   │   └── routing.py        ← 从 routing.py 迁入
│   ├── general/
│   │   └── system.py         ← GENERAL_PROMPT
│   └── workers/
│       ├── morning.py        ← 从 morning.py 迁入
│       ├── stock.py          ← STOCK_ANALYST_PROMPT
│       ├── sector.py         ← SECTOR_ANALYST_PROMPT
│       └── event.py          ← EVENT_ANALYST_PROMPT
└── api/
    ├── __init__.py
    ├── routes.py             ← 重构：用 Depends，新增 /chat/stream
    ├── deps.py               ★ 新增：依赖注入（get_redis / verify_internal_token / build_initial_state）
    └── error_handlers.py     ★ 新增：FastAPI 全局异常处理器

tests/                        ★ 分层
├── conftest.py               ← 顶层 fixtures（保留 mock_redis/mock_node_api/mock_yfinance/mock_tavily）
├── unit/                     ★ 工具函数单测
│   ├── test_utils_sse.py
│   ├── test_utils_parser.py
│   ├── test_utils_message.py
│   ├── test_utils_date.py
│   ├── test_schemas.py
│   ├── test_constants.py
│   └── test_errors.py
├── integration/              ★ Agent + Graph 集成测试
│   ├── test_supervisor.py
│   ├── test_morning_agent.py ← 从根目录迁入
│   ├── test_stock_agent.py
│   ├── test_sector_agent.py
│   ├── test_event_agent.py
│   ├── test_general_agent.py
│   └── test_graph.py
└── e2e/                      ★ 路由端到端测试
    ├── test_chat_message.py
    ├── test_chat_stream.py
    └── test_briefing_morning.py ← 从根目录迁入
```

---

## Task 依赖关系图

```
Task 1 (agents/services/graph/prompts 分层)
  │
  ├── Task 2 (utils/schemas/constants/errors 新增)
  │     │
  │     ├── Task 4 (api/deps 依赖注入)
  │     └── Task 5 (memory/ 持久化)
  │
  ├── Task 3 (tools/base + tests 分层)
  │
  ├── Task 6 (supervisor 测试)  ──┐
  ├── Task 7 (workers 测试)     ──┤
  │                               ├── Task 8 (graph 集成测试)
  │                               │     │
  │                               │     ├── Task 9 (异常降级)
  │                               │     ├── Task 10 (/chat/stream SSE)
  │                               │     └── Task 11 (/chat/message e2e)
  │                               │
  └── Task 12 (文档同步) ← 依赖所有前置
```

---

## Task 1: agents/services/graph/prompts 物理分层

**目标:** 落实用户#5/#6 + A5/A6，把模型工厂迁出 agents，业务 Agent 收敛到 workers，路由函数归入 graph/routers，prompts 与 agents 一一对应分层。本 Task 是后续所有 Task 的基础。

**Files:**
- Create: `src/aistock_agent/services/llm.py`（从 `agents/base.py` 迁移 `get_quick_think` / `get_deep_think`）
- Create: `src/aistock_agent/graph/routers/__init__.py`
- Create: `src/aistock_agent/graph/routers/intent_router.py`（从 `graph/edges.py` 迁入 `route_by_intent` + `VALID_INTENTS`）
- Create: `src/aistock_agent/agents/supervisor/__init__.py`
- Create: `src/aistock_agent/agents/supervisor/node.py`（从 `agents/supervisor.py` 迁入）
- Create: `src/aistock_agent/agents/general/__init__.py`
- Create: `src/aistock_agent/agents/general/node.py`（从 `agents/general_agent.py` 迁入）
- Create: `src/aistock_agent/agents/workers/__init__.py`
- Create: `src/aistock_agent/agents/workers/morning.py`（从 `agents/morning_agent.py` 迁入）
- Create: `src/aistock_agent/agents/workers/stock.py`（从 `agents/stock_analyst.py` 迁入）
- Create: `src/aistock_agent/agents/workers/sector.py`（从 `agents/sector_analyst.py` 迁入）
- Create: `src/aistock_agent/agents/workers/event.py`（从 `agents/event_analyst.py` 迁入）
- Create: `src/aistock_agent/prompts/supervisor/__init__.py`
- Create: `src/aistock_agent/prompts/supervisor/routing.py`（从 `prompts/routing.py` 迁入）
- Create: `src/aistock_agent/prompts/general/__init__.py`
- Create: `src/aistock_agent/prompts/general/system.py`（`GENERAL_PROMPT`）
- Create: `src/aistock_agent/prompts/workers/__init__.py`
- Create: `src/aistock_agent/prompts/workers/morning.py`（从 `prompts/morning.py` 迁入）
- Create: `src/aistock_agent/prompts/workers/stock.py`（`STOCK_ANALYST_PROMPT`）
- Create: `src/aistock_agent/prompts/workers/sector.py`（`SECTOR_ANALYST_PROMPT`）
- Create: `src/aistock_agent/prompts/workers/event.py`（`EVENT_ANALYST_PROMPT`）
- Modify: `src/aistock_agent/graph/builder.py`（更新导入路径：`agents.supervisor.node` / `agents.workers.morning` 等 + `graph.routers.intent_router`）
- Modify: `src/aistock_agent/api/routes.py`（更新 `morning_agent` 导入为 `agents.workers.morning`）
- Delete: `src/aistock_agent/agents/base.py`、`agents/supervisor.py`、`agents/general_agent.py`、`agents/morning_agent.py`、`agents/stock_analyst.py`、`agents/sector_analyst.py`、`agents/event_analyst.py`、`graph/edges.py`、`prompts/routing.py`、`prompts/morning.py`、`prompts/system.py`（迁移后删除）

**Interfaces:**
- Produces: `services.llm.get_quick_think` / `services.llm.get_deep_think`
- Produces: `graph.routers.intent_router.route_by_intent` / `VALID_INTENTS`
- Produces: `agents.supervisor.node.run`
- Produces: `agents.general.node.run`
- Produces: `agents.workers.{morning,stock,sector,event}.run`
- Produces: `agents.workers.morning.stream`（保留 SSE 流式入口）
- Produces: `agents.workers.morning.is_trading_day`（暂留，Task 2 迁到 utils/date）
- Produces: `prompts.supervisor.routing.ROUTING_PROMPT`
- Produces: `prompts.workers.{morning,stock,sector,event}.*` + `prompts.general.system.GENERAL_PROMPT`

**验收标准:**
- [ ] `ruff check src/` 无报错
- [ ] `mypy src/` 无 error
- [ ] `pytest tests/ -v` 全绿（含现有 morning_agent + routes_briefing 测试，验证回归）
- [ ] `python -c "from aistock_agent.graph.builder import compile_graph; compile_graph()"` 可编译图
- [ ] 旧路径 `agents/base.py` / `graph/edges.py` 等已删除，无遗留引用（用 `grep -r "from aistock_agent.agents.base" src/` 验证为空）

**依赖:** 无（基础 Task）

---

## Task 2: 新增基础目录（utils/schemas/constants/errors）

**目标:** 落实 A1/A2/A7/A10，补全通用工具、对外数据模型、常量、异常体系。把 morning_agent 里硬编码的 SSE 映射、LLM 输出解析、消息提取、日期工具抽出，为 Task 4/5/10 提供基础。

**Files:**
- Create: `src/aistock_agent/constants.py`（`SSEEventType` 常量、`INTENT_SET`、`ERROR_CODES`、`TOOL_LABELS` 从 morning_agent 迁入并扩展）
- Create: `src/aistock_agent/utils/__init__.py`
- Create: `src/aistock_agent/utils/sse.py`（`map_langgraph_event_to_sse(event: dict) -> dict | None`，从 `agents.workers.morning.stream` 抽出映射逻辑）
- Create: `src/aistock_agent/utils/parser.py`（`parse_intent(llm_output: str, user_message: str) -> dict`，从 `agents.supervisor.node._parse_intent` 抽出）
- Create: `src/aistock_agent/utils/message.py`（`extract_last_human_message(messages: list) -> str`、`extract_final_ai_response(messages: list) -> str`，消除各 agent 重复的 for 循环）
- Create: `src/aistock_agent/utils/date.py`（`is_trading_day(d: date | None = None) -> bool`，从 morning_agent 迁入）
- Create: `src/aistock_agent/schemas/__init__.py`
- Create: `src/aistock_agent/schemas/chat.py`（`ChatRequest` / `ChatResponse` 从 `api/routes.py` 迁入，补充字段校验）
- Create: `src/aistock_agent/schemas/sse.py`（`SSEEvent` Pydantic 模型，type 取值校验）
- Create: `src/aistock_agent/schemas/agents.py`（各 Agent 的输入/输出 schema，供未来 OpenAPI 文档用）
- Create: `src/aistock_agent/errors/__init__.py`
- Create: `src/aistock_agent/errors/exceptions.py`（`AgentError` 基类、`DataUnavailableError`、`LLMTimeoutError`、`ToolExecutionError`、`RouteError`）
- Modify: `src/aistock_agent/agents/workers/morning.py`（`stream` 改用 `utils.sse.map_langgraph_event_to_sse`，`is_trading_day` 改从 `utils.date` 导入）
- Modify: `src/aistock_agent/agents/supervisor/node.py`（`_parse_intent` 改用 `utils.parser.parse_intent`）
- Modify: 各 workers（stock/sector/event）和 general：消息提取改用 `utils.message`
- Create: `tests/unit/test_utils_sse.py`、`test_utils_parser.py`、`test_utils_message.py`、`test_utils_date.py`、`test_schemas.py`、`test_constants.py`、`test_errors.py`

**Interfaces:**
- Produces: `constants.SSEEventType` / `INTENT_SET` / `TOOL_LABELS`
- Produces: `utils.sse.map_langgraph_event_to_sse`
- Produces: `utils.parser.parse_intent`
- Produces: `utils.message.extract_last_human_message` / `extract_final_ai_response`
- Produces: `utils.date.is_trading_day`
- Produces: `schemas.chat.ChatRequest` / `ChatResponse`、`schemas.sse.SSEEvent`
- Produces: `errors.exceptions.{AgentError, DataUnavailableError, LLMTimeoutError, ToolExecutionError, RouteError}`

**验收标准:**
- [ ] `ruff check src/ && mypy src/` 无 error
- [ ] 7 个 unit 测试文件全部 PASS
- [ ] morning_agent 的 stream 测试仍全绿（验证 utils.sse 抽出后行为一致）
- [ ] supervisor 的 _parse_intent 改用 utils.parser 后，原行为不变（手动对比 5 类意图输出）
- [ ] `constants.py` 中无 magic string（所有 SSE 事件类型用常量引用）

**依赖:** Task 1（迁移完成后再抽出 utils）

---

## Task 3: tools/base.py + tests 分层

**目标:** 落实 A11/A12，补齐通用 tool 基类（统一错误处理、参数校验、日志），并把 tests 分层到 unit/integration/e2e。

**Files:**
- Create: `src/aistock_agent/tools/base.py`（`class BaseToolMixin`：统一 try-catch 包装、错误日志、参数校验 hook；提供 `safe_tool_call` 装饰器）
- Modify: `src/aistock_agent/tools/stock_tools.py` / `sector_tools.py` / `news_tools.py` / `market_tools.py`（各 @tool 函数改用 BaseToolMixin 或 safe_tool_call 装饰器，统一异常返回降级文本）
- Create: `tests/unit/`、`tests/integration/`、`tests/e2e/` 目录（含 `__init__.py`）
- Move: `tests/test_morning_agent.py` → `tests/integration/test_morning_agent.py`
- Move: `tests/test_routes_briefing.py` → `tests/e2e/test_briefing_morning.py`
- Move: `tests/test_stock_tools.py` / `test_sector_tools.py` / `test_news_tools.py` / `test_market_tools.py` → `tests/unit/`
- Modify: `tests/conftest.py`（保留顶层 fixtures，新增 `tests/integration/conftest.py` 和 `tests/e2e/conftest.py` 按需补充）
- Modify: `pyproject.toml` 或 `pytest.ini`（配置 `testpaths = ["tests"]` + `python_files = ["test_*.py"]`，确保分层后 pytest 仍能发现）

**Interfaces:**
- Produces: `tools.base.BaseToolMixin` / `safe_tool_call`
- Produces: tests 三层目录结构

**验收标准:**
- [ ] `pytest tests/ -v` 全绿（迁移后所有测试仍能跑通）
- [ ] `pytest tests/unit/ -v` 只跑工具单测
- [ ] `pytest tests/integration/ -v` 只跑 agent + graph 测试
- [ ] `pytest tests/e2e/ -v` 只跑路由端到端测试
- [ ] tools/base.py 的 mixin 在至少 2 个 tool 上应用，错误处理逻辑统一

**依赖:** Task 1（tools 导入路径稳定后再加 base）

---

## Task 4: api/deps.py 依赖注入抽离

**目标:** 落实 A8，把 `_verify_internal_token`、state 构造、Redis 客户端获取抽到 `api/deps.py`，用 FastAPI Depends 复用，为 Task 10 的 /chat/stream 和 Phase 5 的 lifespan 做准备。

**Files:**
- Create: `src/aistock_agent/api/deps.py`
  - `verify_internal_token(x_internal_token: str | None = Header(None)) -> None`（从 routes.py 迁入）
  - `build_initial_state(message: str, session_id: str | None, user_id: str | None, favorites: list[str]) -> dict`（从 routes.py 的 /chat/message 抽出）
  - `get_redis_client() -> aioredis.Redis`（暂用 from_url，Phase 5 改 lifespan 池）
- Modify: `src/aistock_agent/api/routes.py`
  - `/chat/message` 用 `Depends(verify_internal_token)` + `build_initial_state`
  - `/briefing/morning` 不需要 token（公开接口），但 state 构造可复用
  - 删除内联的 `_verify_internal_token` 和 `ChatRequest`/`ChatResponse`（已迁到 schemas/chat.py）
- Modify: `src/aistock_agent/schemas/chat.py`（确认 ChatRequest/ChatResponse 已在此）

**Interfaces:**
- Produces: `api.deps.verify_internal_token` / `build_initial_state` / `get_redis_client`
- Consumes: `schemas.chat.ChatRequest` / `ChatResponse`

**验收标准:**
- [ ] `ruff check src/ && mypy src/` 无 error
- [ ] `/chat/message` 端点签名包含 `Depends(verify_internal_token)`
- [ ] 缺失 `X-Internal-Token` 时 `/chat/message` 返回 403
- [ ] `/briefing/morning` 仍可无 token 访问（公开接口）
- [ ] 现有 `test_routes_briefing.py` 全绿

**依赖:** Task 2（依赖 schemas/chat.py）

---

## Task 5: memory/ 持久化记忆模块

**目标:** 落实 A3，落地 LangGraph checkpointer，使多轮对话可恢复、会话历史可查询、用户偏好可记忆。这是 Phase 4 验收"完整消息流程"的多轮对话基础。

**Files:**
- Create: `src/aistock_agent/memory/__init__.py`
- Create: `src/aistock_agent/memory/checkpointer.py`
  - `get_checkpointer() -> BaseCheckpointSaver`：根据 `config.checkpointer_backend`（"sqlite" / "redis"）返回对应 saver
  - 开发默认 `SqliteSaver`（from_path），生产 `RedisSaver`
  - 单例缓存，避免重复初始化
- Create: `src/aistock_agent/memory/session_store.py`
  - `async def save_session(session_id: str, messages: list) -> None`
  - `async def load_session(session_id: str) -> list`
  - 基于 Redis，key 格式 `session:{session_id}:messages`
- Create: `src/aistock_agent/memory/preferences.py`
  - `async def get_user_favorites(user_id: str) -> list[str]`
  - `async def set_user_favorites(user_id: str, symbols: list[str]) -> None`
  - 复用 services/cache.py（Task 2 抽出的 B5）
- Modify: `src/aistock_agent/graph/builder.py`（`compile_graph()` 接受可选 `checkpointer` 参数，默认挂载 `get_checkpointer()`）
- Modify: `src/aistock_agent/api/routes.py`（`/chat/message` 用 `req.session_id` 作为 thread_id 调用 graph）
- Modify: `src/aistock_agent/config.py`（新增 `checkpointer_backend: str = "sqlite"`、`sqlite_path: str = ".langgraph.db"`）
- Create: `tests/integration/test_memory.py`（checkpointer 多轮对话恢复测试 + session_store 读写测试 + preferences 读写测试）

**Interfaces:**
- Produces: `memory.checkpointer.get_checkpointer`
- Produces: `memory.session_store.save_session` / `load_session`
- Produces: `memory.preferences.get_user_favorites` / `set_user_favorites`
- Consumes: `services.cache`（Task 2 产出）

**验收标准:**
- [ ] `compile_graph().ainvoke(state, config={"configurable": {"thread_id": "test_session"}})` 两次调用，第二次能拿到第一次的 messages
- [ ] `session_store.save_session` → `load_session` 数据一致
- [ ] checkpointer backend 可通过 env 切换（sqlite/redis）
- [ ] `pytest tests/integration/test_memory.py -v` 全绿
- [ ] 现有 graph 测试不破坏（checkpointer 为可选参数）

**依赖:** Task 2（依赖 services/cache.py）

---

## Task 6: Supervisor 测试 + 健壮性强化

**目标:** 落实 D1，补齐 supervisor 意图分类测试，强化边界处理（空消息、LLM 输出无法解析、多模态内容）。

**Files:**
- Create: `tests/integration/test_supervisor.py`
  - 5 类意图分类测试（morning/stock/sector/event/general）
  - 6 位股票代码提取（含边界：连续 7 位数字、字母夹数字）
  - BK 板块码提取（大小写、BK 后位数）
  - 空消息降级（返回 intent="general"）
  - LLM 输出无法解析降级（输出含非类别词时 fallback）
  - 多模态消息内容（content 为 list 时转 str 不崩溃）
- Modify: `src/aistock_agent/agents/supervisor/node.py`（用 `utils.parser.parse_intent`，补强边界）
- Modify: `src/aistock_agent/utils/parser.py`（按测试反馈补强 `parse_intent` 边界）

**Interfaces:**
- Consumes: `utils.parser.parse_intent`、`agents.supervisor.node.run`
- Produces: supervisor 测试套件

**验收标准:**
- [ ] `pytest tests/integration/test_supervisor.py -v` 全绿（至少 8 个测试用例）
- [ ] 边界用例覆盖：空消息、无法解析输出、多模态内容、连续 7 位数字、BK 大小写
- [ ] supervisor.run 对所有 5 类意图返回正确的 intent 字段

**依赖:** Task 1（supervisor 迁移完成）、Task 2（utils.parser 就绪）

---

## Task 7: 4 个业务 Agent 单元测试

**目标:** 落实 D2，为 morning/stock/sector/event/general 5 个 Agent 补齐单元测试（morning 已有，迁入 integration 即可）。

**Files:**
- Create: `tests/integration/test_stock_agent.py`
  - 工具集绑定验证（mock create_react_agent，断言传入的 tools 列表）
  - SystemMessage 注入验证
  - final_response 提取验证（mock 返回多条消息，取最后一条 AI）
  - symbol 缺失时返回提示文本
  - 异常路径（LLM 失败降级）—— 此项可留到 Task 9 补
- Create: `tests/integration/test_sector_agent.py`（同上，验证 tag_code 默认值逻辑）
- Create: `tests/integration/test_event_agent.py`（同上）
- Create: `tests/integration/test_general_agent.py`（同上，验证用 quick_think）
- Move + Modify: `tests/integration/test_morning_agent.py`（从根目录迁入，验证 stream + run + is_trading_day 三个入口）
- Modify: 各 workers agent（按测试反馈小修，如统一用 `utils.message.extract_final_ai_response`）

**Interfaces:**
- Consumes: `conftest.mock_redis` / `mock_node_api`、`unittest.mock.patch` create_react_agent
- Produces: 5 个 agent 测试套件

**验收标准:**
- [ ] `pytest tests/integration/ -v` 全绿
- [ ] 每个 agent 至少 4 个测试：工具绑定、SystemMessage 注入、response 提取、入口校验
- [ ] 所有 agent 用 mock，不依赖真实网络/LLM/Redis

**依赖:** Task 1（agents 迁移完成）、Task 2（utils.message 就绪）

---

## Task 8: Graph 集成测试（验收关键证据）

**目标:** 落实 D3，验证"完整消息流程：输入→路由→工具调用→回复"在 5 条路径上端到端跑通。这是 Phase 4 验收的核心证据。

**Files:**
- Create: `tests/integration/test_graph.py`
  - `test_graph_routes_morning`：mock supervisor 返回 intent=morning，验证 graph 路由到 morning_agent 节点
  - `test_graph_routes_stock`：同上 stock
  - `test_graph_routes_sector`：同上 sector
  - `test_graph_routes_event`：同上 event
  - `test_graph_routes_general`：同上 general
  - `test_graph_routes_unknown_intent_to_general`：未知 intent fallback
  - `test_graph_full_flow_with_mock_agents`：完整流程，mock 各 agent.run 返回固定 final_response，验证 graph.ainvoke 返回正确结果
- Modify: `src/aistock_agent/graph/builder.py`（如发现可测试性问题，小修；不改变拓扑）

**Interfaces:**
- Consumes: `graph.builder.compile_graph`、`graph.routers.intent_router.route_by_intent`
- Produces: graph 集成测试套件

**验收标准:**
- [ ] `pytest tests/integration/test_graph.py -v` 全绿（至少 7 个测试）
- [ ] 5 条路径全覆盖：morning/stock/sector/event/general
- [ ] 完整流程测试验证 `result.get("final_response")` 不为空
- [ ] 未知 intent 兜底到 general_agent

**依赖:** Task 6、Task 7（agent 测试就绪后再做集成）

---

## Task 9: 异常降级（AGENTS.md 合规）

**目标:** 落实 E2 + AGENTS.md 要求"Tool 失败返回降级文本，不抛异常中断图执行"。各 agent 的 `run()` 加顶层 try-catch，分类捕获异常并返回降级文本。

**Files:**
- Modify: `src/aistock_agent/agents/workers/morning.py`（`run` 加 try-catch，捕获 `ToolExecutionError` / `LLMTimeoutError` / `Exception`，返回 `{"final_response": "晨报生成失败，请稍后重试"}` 等）
- Modify: `src/aistock_agent/agents/workers/stock.py`（同上）
- Modify: `src/aistock_agent/agents/workers/sector.py`（同上）
- Modify: `src/aistock_agent/agents/workers/event.py`（同上）
- Modify: `src/aistock_agent/agents/general/node.py`（同上）
- Modify: `src/aistock_agent/tools/base.py`（`safe_tool_call` 装饰器捕获工具异常，抛 `ToolExecutionError`，由 agent 层捕获）
- Create: `tests/integration/test_agent_fallback.py`（每个 agent 一个异常路径测试，mock 工具抛异常，验证返回降级文本且不中断图）

**Interfaces:**
- Consumes: `errors.exceptions.{ToolExecutionError, LLMTimeoutError, DataUnavailableError}`
- Produces: 各 agent 的异常降级行为

**验收标准:**
- [ ] `pytest tests/integration/test_agent_fallback.py -v` 全绿
- [ ] 每个 agent 的工具失败测试返回降级文本，不抛异常
- [ ] graph.ainvoke 在 agent 异常时仍能正常返回（不中断）
- [ ] 降级文本符合 AGENTS.md 规范（标注"暂不可用"，不猜测数据）

**依赖:** Task 7（agent 测试就绪后补异常路径）、Task 8（验证图不中断）

---

## Task 10: SSE 流式对话接口 /chat/stream

**目标:** 落实 E1，新增 `POST /chat/stream` SSE 端点，复用 morning_agent.stream 的事件映射模式（已抽到 utils/sse.py），走 graph.astream_events。

**Files:**
- Modify: `src/aistock_agent/api/routes.py`
  - 新增 `@router.post("/chat/stream")` 端点，返回 `EventSourceResponse`
  - 内部用 `compile_graph().astream_events(initial_state, version="v2")`
  - 用 `utils.sse.map_langgraph_event_to_sse` 统一映射
  - 加 `Depends(verify_internal_token)`
- Modify: `src/aistock_agent/utils/sse.py`（扩展 `map_langgraph_event_to_sse`，支持 graph 层事件，复用 morning 的 tool_start/tool_end/llm_start/text/done/error）
- Modify: `src/aistock_agent/schemas/chat.py`（新增 `ChatStreamRequest`，与 ChatRequest 区别仅在不返回 ChatResponse）
- Create: `tests/e2e/test_chat_stream.py`
  - Content-Type 校验（text/event-stream）
  - SSE 事件序列测试（mock graph.astream_events，验证 tool_start/text/done 序列）
  - error 事件传递测试
  - 5 类意图至少各跑一个用例（mock supervisor 返回不同 intent）

**Interfaces:**
- Produces: `POST /chat/stream` 返回 `text/event-stream`
- Consumes: `graph.builder.compile_graph`、`utils.sse.map_langgraph_event_to_sse`、`api.deps.verify_internal_token`

**验收标准:**
- [ ] `pytest tests/e2e/test_chat_stream.py -v` 全绿
- [ ] Content-Type 为 `text/event-stream`
- [ ] SSE 数据行可解析为 JSON，事件序列符合约定
- [ ] error 事件正确传递
- [ ] 缺失 token 返回 403

**依赖:** Task 4（deps）、Task 8（graph 集成验证）

---

## Task 11: /chat/message 非流式端到端测试

**目标:** 落实 D4，为现有 `/chat/message` 端点补齐端到端测试，验证 5 类意图完整跑通。

**Files:**
- Create: `tests/e2e/test_chat_message.py`
  - `test_chat_message_stock_intent`：mock supervisor 返回 stock，mock stock_agent 返回固定文本，验证响应
  - `test_chat_message_sector_intent`：同上 sector
  - `test_chat_message_event_intent`：同上 event
  - `test_chat_message_morning_intent`：同上 morning
  - `test_chat_message_general_intent`：同上 general
  - `test_chat_message_missing_token_403`
  - `test_chat_message_empty_message`
- Modify: `src/aistock_agent/api/routes.py`（按测试反馈小修）

**Interfaces:**
- Consumes: `api.routes.chat_message`、`api.deps.verify_internal_token`
- Produces: /chat/message 端到端测试套件

**验收标准:**
- [ ] `pytest tests/e2e/test_chat_message.py -v` 全绿
- [ ] 5 类意图端到端全覆盖
- [ ] 鉴权失败返回 403
- [ ] 响应体符合 `ChatResponse` schema

**依赖:** Task 8（graph 集成验证）

---

## Task 12: 文档同步

**目标:** 落实 F1/F2，更新 refactor-plan.md 和 AGENTS.md，反映 Phase 4 实际目录结构、intent 命名简化、Phase 4 完成状态。

**Files:**
- Modify: `docs/refactor-plan.md`
  - 第 10 节 Phase 表格：Phase 4 状态从"⏳ 待开始"改为"✅ 完成"，更新核心产出描述（含 utils/schemas/memory 等）
  - 第 4 节目录结构：替换为重构后的 AFTER 目录树
  - 第 7 节 Agent 设计：更新 agent 文件路径（agents/workers/morning.py 等）
  - 第 12 节：确认所有待确认事项已关闭
  - 头部状态字段：版本 v2.1，日期 2026-07-07，状态"Phase 4 完成"
- Modify: `AGENTS.md`（aistock-agent-py 根目录）
  - 目录结构章节：替换为重构后结构
  - 常用命令章节：补充 `pytest tests/unit/` / `tests/integration/` / `tests/e2e/`
  - 关键约束章节：补充"agents 物理分层：supervisor/ + general/ + workers/，禁止混放"
  - 新增章节："异常降级规范"，说明各 agent 必须有 try-catch
- Modify: `project_memory.md`（`c:\Users\37588\.trae-cn\memory\projects\-d-ai_stock_app\project_memory.md`）
  - 追加 Phase 4 完成的硬约束（如"agents 必须按 supervisor/general/workers 分层"）

**验收标准:**
- [ ] refactor-plan.md 第 10 节 Phase 4 标"✅ 完成"
- [ ] refactor-plan.md 目录结构 = 实际代码目录结构
- [ ] AGENTS.md 目录结构 = 实际代码目录结构
- [ ] project_memory.md 追加 Phase 4 硬约束

**依赖:** 所有前置 Task 完成

---

## Phase 4 整体验收标准

完成以下全部检查后，Phase 4 视为通过：

- [ ] **目录结构**：`src/aistock_agent/` 实际结构 = AFTER 目录树
- [ ] **测试覆盖**：`pytest tests/ -v` 全绿，且分层后 unit/integration/e2e 各自可独立运行
- [ ] **类型检查**：`ruff check src/ && mypy src/` 无 error
- [ ] **回归基线**：原 `test_morning_agent.py` 和 `test_routes_briefing.py` 迁移后仍全绿
- [ ] **对话闭环**：`/chat/message` 端到端测试覆盖 5 类意图
- [ ] **流式对话**：`/chat/stream` SSE 端点可用，事件序列正确
- [ ] **异常降级**：各 agent 异常路径有测试，不中断图执行
- [ ] **持久化记忆**：checkpointer 多轮对话可恢复
- [ ] **文档同步**：refactor-plan.md + AGENTS.md + project_memory.md 全部更新

---

## 风险与决策点

### 风险 1：迁移期间导入路径破坏
- **风险**：Task 1 大量文件移动，可能遗漏导入更新导致 ImportError
- **缓解**：Task 1 完成后立即跑 `pytest tests/ -v` 作为回归门禁，全绿才进入 Task 2
- **回滚**：git commit 粒度按 Task 切分，单个 Task 失败可 revert

### 风险 2：memory/ checkpointer 引入新依赖
- **风险**：`langgraph.checkpoint.redis` / `langgraph.checkpoint.sqlite` 可能未在 pyproject.toml
- **缓解**：Task 5 Step 1 先确认依赖，缺失则补充到 `pyproject.toml` 的 dependencies
- **决策**：开发默认 sqlite（零配置），生产用 redis（与现有 Redis 共用）

### 风险 3：utils/sse.py 抽出后 morning 行为变化
- **风险**：morning_agent.stream 的事件映射逻辑抽出后，可能引入细微行为差异
- **缓解**：Task 2 完成后必须重跑 `test_morning_agent.py` 全部用例，逐事件对比

### 风险 4：tests 分层后 pytest 配置失效
- **风险**：迁移测试文件后 pytest 可能找不到
- **缓解**：Task 3 Step 1 先配置 pytest.ini/testpaths，再迁移文件

### 决策点：services/llm.py 是否保留旧路径 re-export
- **建议**：不保留。Task 1 完成后用 `grep -r "from aistock_agent.agents.base" src/ tests/` 确认无外部引用，直接删除 `agents/base.py`。若发现外部引用，补一个 deprecation re-export 一个版本。

### 决策点：prompts/general/system.py 是否拆分
- **建议**：保留 `SYSTEM_PROMPT` 作为基础常量在 `prompts/general/system.py`，各 workers 的 prompt 在 `prompts/workers/*.py` 里 import `SYSTEM_PROMPT` 后拼接。不单独拆 `prompts/workers/_base.py`，避免过度抽象。

### 决策点：constants.py vs types/ 目录
- **建议**：本 Phase 用单文件 `constants.py`，不建 `types/` 目录。若 Phase 5 类型增多再考虑目录化。

---

## 实施顺序建议

按 Task 依赖关系图，推荐顺序：

```
Task 1 → Task 2 → Task 3
              ↓
         Task 4（可与 Task 3 并行）
         Task 5（可与 Task 3/4 并行）
              ↓
         Task 6 → Task 7 → Task 8
                                ↓
                          Task 9
                          Task 10
                          Task 11
                                ↓
                          Task 12
```

总工作量预估：12 个 Task，每个 Task 0.5-1 天，总计约 6-10 个工作日。

---

*本计划在实施过程中持续更新。架构变更需同步修改本文档及 refactor-plan.md。*
