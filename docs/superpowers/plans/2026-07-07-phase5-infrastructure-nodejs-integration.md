# Phase 5: 基础设施增强 + Node.js 接入 + 生产可用 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按 Task 推进。Steps 用 checkbox（`- [ ]`）跟踪。

**Goal:** 在 Phase 4 重构后的分层骨架上，补齐生产可用所需的基础设施（lifespan 资源管理、config 增强、可观测性、健康检查、中间件），完成 Node.js 侧 `/internal/*` 数据接口和 Python 侧对应 Tools 的实现，打通 Express → Python 反代链路，端到端验证全流程，并产出 `AGENT_STANDARDS.md` 开发标准文档。

**Architecture:** Node.js 保留数据层与 HTTP 接入职责，新增 8 个 `/internal/*` 接口供 Python 回调；Python 侧补齐对应 `@tool` 函数，通过 `services/data_client.py` 调用；Express 配置 `/api/agent/*` 反代到 Python FastAPI，SSE 流式透传。Python 服务引入 lifespan 管理全局资源（Redis 连接池 + httpx AsyncClient），structlog + LangGraph callback 提供可观测性。

**Tech Stack:** FastAPI `lifespan`、`structlog==25.3.0`、`httpx==0.28.1`、`redis==5.2.1`（连接池）、Express `http-proxy-middleware`、`langgraph.callbacks`、LangSmith（可选）

## Global Constraints

- Python ≥ 3.11
- 本 Phase 不新增业务 Agent，聚焦基础设施 + 数据接口 + 反代 + 文档
- Node.js `/internal/*` 接口必须携带 `X-Internal-Token` 鉴权，不对外暴露
- Python 侧 Tools 必须复用 `tools/base.py`（Phase 4 产出）的 `BaseToolMixin` / `safe_tool_call`
- lifespan 管理的资源必须支持优雅关闭（启动初始化、关闭释放、异常不崩溃）
- 可观测性不得侵入业务逻辑（用 callback / middleware 解耦）
- `AGENT_STANDARDS.md` 必须覆盖 refactor-plan.md 第 11 节大纲的全部 8 条规范
- 跨仓库修改遵循 aistock-workflow rules 的"跨端同步检查"步骤

---

## 跨仓库影响范围

本 Phase 涉及 3 个仓库的协同改动：

| 仓库 | 改动范围 | 主要 Task |
|------|----------|-----------|
| `aistock-agent-py` | lifespan / config / observability / middleware / tools 补齐 / 健康检查 | Task 1-5, 7, 9 |
| `aistock-app-api` | 新增 `/internal/*` 路由 + Express 反代配置 | Task 6, 8 |
| `aistock-app-frontend` | 无直接改动（受益于反代透传） | — |

---

## Node.js `/internal/*` 接口清单（Task 6 + Task 7 对应表）

| Node.js 接口 | 数据源 Service | Python Tool | Tool 文件 | 用途 |
|--------------|---------------|-------------|-----------|------|
| `GET /internal/wind-leaders` | WindLeaderService | `get_wind_leaders` | `tools/sector_tools.py` | 风口龙头数据 |
| `GET /internal/monitor/:symbol` | StockMonitorService | `get_stock_monitor` | `tools/monitor_tools.py` | 个股异动数据 |
| `GET /internal/monitor/alerts` | StockMonitorService | `get_alert_history` | `tools/monitor_tools.py` | 异动历史 |
| `GET /internal/tenx/score/:symbol` | TenxScoreService | `get_tenx_score` | `tools/tenx_tools.py` | 十倍股评分详情 |
| `GET /internal/tenx/top` | TenxScoreService | `get_tenx_top_stocks` | `tools/tenx_tools.py` | 十倍股排行 |
| `GET /internal/graph/concepts` | IndustryKGService | `get_concepts` | `tools/graph_tools.py`（新建） | 产业链概念列表 |
| `GET /internal/graph/:concept` | IndustryKGService | `get_graph_by_concept` | `tools/graph_tools.py` | 产业链图谱数据 |
| `GET /internal/institution-research` | HotBurstService | `get_hot_burst` | `tools/hot_burst_tools.py`（新建） | 机构调研共振检测 |
| `GET /internal/institution-research/history` | HotBurstService | `get_hot_burst_history` | `tools/hot_burst_tools.py` | 共振历史记录 |

> 注：refactor-plan.md 第 8 节列出的前 6 个接口（quote/flow/leader/news/search/fulltext/forecast）已在 Phase 2 实现，本 Phase 只补齐剩余 8 个标注"★ 新增"的接口。

---

## Task 1: lifespan 应用生命周期管理

**目标:** 落实 B1，用 FastAPI lifespan 在应用启动时初始化 Redis 连接池 + httpx AsyncClient，全局复用，关闭时优雅释放。消除 morning_agent 每次请求 `from_url` 的性能问题。

**Files:**
- Create: `src/aistock_agent/services/redis_pool.py`
  - `class RedisPool`：单例，`async def get_client() -> aioredis.Redis`、`async def close() -> None`
  - 基于 `redis.asyncio.ConnectionPool`，max_connections 可配
- Create: `src/aistock_agent/services/http_client.py`
  - `class HttpClientPool`：单例，`async def get_client() -> httpx.AsyncClient`、`async def close() -> None`
  - 默认超时 10s，可配
- Modify: `src/aistock_agent/main.py`
  - 新增 `@asynccontextmanager async def lifespan(app: FastAPI)`
  - 启动：`await RedisPool.init(settings.redis_url)` + `await HttpClientPool.init()`
  - 关闭：`await RedisPool.close()` + `await HttpClientPool.close()`
  - `FastAPI(lifespan=lifespan)`
- Modify: `src/aistock_agent/api/deps.py`（`get_redis_client` 改用 `RedisPool.get_client()`，移除 from_url）
- Modify: `src/aistock_agent/services/cache.py`（Phase 4 产出，改用 `RedisPool.get_client()`）
- Modify: `src/aistock_agent/services/data_client.py`（改用 `HttpClientPool.get_client()`）
- Modify: `src/aistock_agent/agents/workers/morning.py`（`_get_cached_briefing` / `_set_cached_briefing` 改用 `services.cache`，移除内联 from_url）

**Interfaces:**
- Produces: `services.redis_pool.RedisPool`、`services.http_client.HttpClientPool`
- Produces: `main.lifespan`

**验收标准:**
- [ ] `uvicorn aistock_agent.main:app --reload` 启动日志可见 "RedisPool initialized" + "HttpClientPool initialized"
- [ ] 关闭时日志可见 "RedisPool closed" + "HttpClientPool closed"
- [ ] `/briefing/morning` 两次调用，Redis 连接数不递增（用 `redis-cli INFO clients` 验证）
- [ ] `pytest tests/ -v` 全绿（lifespan 在测试中可用 `httpx.ASGITransport` 触发）
- [ ] 现有 morning_agent 测试不破坏

**依赖:** Phase 4 完成（services/cache.py、api/deps.py 就绪）

---

## Task 2: config.py 增强

**目标:** 落实 B2，扩展 config 支持多模型参数、连接池参数、HTTP 超时、日志级别、LangSmith 开关。

**Files:**
- Modify: `src/aistock_agent/config.py`
  - 新增字段：
    - `quick_think_temperature: float = 0.1`、`quick_think_max_tokens: int = 2000`
    - `deep_think_temperature: float = 0.3`、`deep_think_max_tokens: int = 4000`
    - `redis_max_connections: int = 10`
    - `http_timeout_seconds: float = 10.0`
    - `log_level: str = "INFO"`
    - `langsmith_enabled: bool = False`、`langsmith_api_key: str | None = None`、`langsmith_project: str = "aistock-agent"`
    - `node_api_base_url: str = "http://localhost:3000"`（确认现有字段名）
- Modify: `src/aistock_agent/services/llm.py`（`get_quick_think` / `get_deep_think` 读取 temperature/max_tokens from config）
- Modify: `.env.example`（补充新增字段示例）
- Create: `tests/unit/test_config.py`（验证字段默认值 + env 覆盖）

**Interfaces:**
- Produces: 扩展后的 `config.Settings`

**验收标准:**
- [ ] `pytest tests/unit/test_config.py -v` 全绿
- [ ] env 设置 `QUICK_THINK_TEMPERATURE=0.5` 时，`get_quick_think()` 的 ChatOpenAI temperature 为 0.5
- [ ] `.env.example` 含全部新增字段
- [ ] `mypy src/` 无 error

**依赖:** Task 1（config 字段被 lifespan 引用）

---

## Task 3: 健康检查增强

**目标:** 落实 B4，`/health` 升级为返回依赖连通性状态，区分 liveness 和 readiness。

**Files:**
- Modify: `src/aistock_agent/api/routes.py`
  - `GET /health`（liveness）：返回 `{"status": "ok"}`，不检查依赖（K8s livenessProbe 用）
  - `GET /health/ready`（readiness）：返回 `{"status": "ok"|"degraded", "checks": {redis, node_api, llm}}`
    - redis：`RedisPool.get_client().ping()`
    - node_api：`HttpClientPool.get_client().get(f"{node_api_base_url}/internal/health")`（Node.js 侧需加 `/internal/health`）
    - llm：可选检查（避免消耗 token，默认跳过，通过 env `HEALTH_CHECK_LLM=true` 开启）
- Modify: `aistock-app-api`（Node.js 侧新增 `GET /internal/health` 简单返回 ok，供 Python 探测）
- Create: `tests/e2e/test_health.py`（mock 依赖，验证 ok/degraded 状态）

**Interfaces:**
- Produces: `GET /health`（liveness）、`GET /health/ready`（readiness）

**验收标准:**
- [ ] `pytest tests/e2e/test_health.py -v` 全绿
- [ ] `/health` 始终返回 200 + `{"status":"ok"}`
- [ ] `/health/ready` 在 Redis/Node.js 可达时返回 200，不可达时返回 503 + degraded
- [ ] Node.js `/internal/health` 端点存在且返回 200

**依赖:** Task 1（lifespan 提供 RedisPool/HttpClientPool）

---

## Task 4: observability/ 可观测性

**目标:** 落实 B3，引入 structlog 结构化日志 + LangGraph callback handler + Token 用量统计 + LangSmith 集成（可选）。

**Files:**
- Create: `src/aistock_agent/observability/__init__.py`
- Create: `src/aistock_agent/observability/logging.py`
  - `setup_logging(level: str) -> None`：配置 structlog，JSON 输出，含 timestamp/level/event/request_id
  - `get_logger(name: str) -> structlog.BoundLogger`
- Create: `src/aistock_agent/observability/callback.py`
  - `class TokenUsageCallback(BaseCallbackHandler)`：记录 on_llm_start / on_llm_end 的 token 用量
  - `class AgentTraceCallback(BaseCallbackHandler)`：记录工具调用、agent 步骤，供 LangSmith 追踪
- Create: `src/aistock_agent/observability/metrics.py`
  - `class MetricsCollector`：累计 token 用量、调用次数、错误率
  - `get_metrics() -> dict`：供 `/metrics` 端点暴露（可选）
- Modify: `src/aistock_agent/main.py`（启动时调用 `setup_logging(settings.log_level)`）
- Modify: `src/aistock_agent/services/llm.py`（`get_quick_think` / `get_deep_think` 挂载 callback，若 `langsmith_enabled` 则开启 tracing）
- Modify: `src/aistock_agent/graph/builder.py`（compile 时传入 callback）
- Create: `tests/unit/test_observability_logging.py`、`test_observability_callback.py`

**Interfaces:**
- Produces: `observability.logging.setup_logging` / `get_logger`
- Produces: `observability.callback.TokenUsageCallback` / `AgentTraceCallback`
- Produces: `observability.metrics.MetricsCollector`

**验收标准:**
- [ ] `pytest tests/unit/test_observability_*.py -v` 全绿
- [ ] 启动服务后日志为 JSON 格式，含 timestamp/level/event 字段
- [ ] LLM 调用后 `MetricsCollector` 记录了 token 用量
- [ ] `LANGSMITH_ENABLED=true` 时，LangSmith 后台可见 trace（手动验证）
- [ ] 业务逻辑代码无 structlog 侵入（只通过 callback 注入）

**依赖:** Task 2（config 提供 log_level/langsmith 字段）

---

## Task 5: api/middleware.py 中间件

**目标:** 落实 A9，新增请求 ID 注入、结构化访问日志、CORS 中间件。

**Files:**
- Create: `src/aistock_agent/api/middleware.py`
  - `@asynccontextmanager async def request_id_middleware(request, call_next)`：从 header 取或生成 `X-Request-ID`，注入 structlog contextvar，响应回写 header
  - `async def access_log_middleware(request, call_next)`：记录 method/path/status/duration，用 structlog
  - `def setup_middleware(app: FastAPI)`：注册上述 + CORSMiddleware（origins 从 config 读取）
- Modify: `src/aistock_agent/main.py`（调用 `setup_middleware(app)`）
- Modify: `src/aistock_agent/config.py`（新增 `cors_origins: list[str] = ["*"]`）
- Create: `tests/e2e/test_middleware.py`（验证 X-Request-ID 回写、CORS header、访问日志输出）

**Interfaces:**
- Produces: `api.middleware.setup_middleware`、`request_id_middleware`、`access_log_middleware`

**验收标准:**
- [ ] `pytest tests/e2e/test_middleware.py -v` 全绿
- [ ] 每个响应包含 `X-Request-ID` header
- [ ] OPTIONS 预检请求返回 CORS header
- [ ] 访问日志包含 method/path/status/duration/request_id
- [ ] structlog contextvar 在请求结束后清理（无跨请求污染）

**依赖:** Task 4（依赖 structlog 配置）

---

## Task 6: Node.js 新增 `/internal/*` 接口

**目标:** 在 `aistock-app-api` 侧补齐 8 个 `/internal/*` 接口，对接现有 Service，携带 `X-Internal-Token` 鉴权。

**Files:**
- Modify: `aistock-app-api/src/modules/internal/internal.routes.ts`（或对应路由文件，确认现有结构）
  - 新增 8 个 GET 路由（见上方接口清单表）
  - 全部走 `verifyInternalToken` 中间件（复用现有）
- Modify: `aistock-app-api/src/modules/internal/internal.controller.ts`（或对应 controller）
  - 每个接口调用对应 Service 方法
  - 错误处理：Service 失败返回 502 + 错误信息
- Create: `aistock-app-api/src/modules/internal/__tests__/internal.routes.spec.ts`（每个接口至少一个测试）
- Modify: `aistock-app-api/src/modules/internal/internal.controller.ts`
  - 新增 `GET /internal/health`（供 Python 探测，Task 3 依赖）

**接口实现要点:**
- `GET /internal/wind-leaders` → `WindLeaderService.getWindLeaders()`
- `GET /internal/monitor/:symbol` → `StockMonitorService.getMonitorData(symbol)`
- `GET /internal/monitor/alerts` → `StockMonitorService.getAlertHistory(query)`
- `GET /internal/tenx/score/:symbol` → `TenxScoreService.getScore(symbol)`
- `GET /internal/tenx/top` → `TenxScoreService.getTopStocks(query)`
- `GET /internal/graph/concepts` → `IndustryKGService.getConcepts()`
- `GET /internal/graph/:concept` → `IndustryKGService.getGraphByConcept(concept)`
- `GET /internal/institution-research` → `HotBurstService.getHotBurst(query)`
- `GET /internal/institution-research/history` → `HotBurstService.getHotBurstHistory(query)`

**Interfaces:**
- Produces: 8 个 Node.js `/internal/*` 端点 + `/internal/health`

**验收标准:**
- [ ] `npm test`（或对应命令）全绿，8 个接口各有测试
- [ ] 缺失 `X-Internal-Token` 返回 403
- [ ] 每个 Service 调用失败时返回 502 + 错误信息
- [ ] `/internal/health` 返回 200 + `{"status":"ok"}`

**依赖:** 无（可与 Python 侧 Task 7 并行开发，mock 对端）

---

## Task 7: Python 侧对应 Tools 实现

**目标:** 在 `aistock-agent-py` 侧补齐 9 个 `@tool` 函数，通过 `services/data_client.py` 调用 Node.js `/internal/*`，复用 `tools/base.py` 的 `BaseToolMixin`。

**Files:**
- Modify: `src/aistock_agent/tools/sector_tools.py`
  - 新增 `get_wind_leaders() -> str`：调用 `GET /internal/wind-leaders`
- Modify: `src/aistock_agent/tools/monitor_tools.py`（原占位文件实现）
  - `get_stock_monitor(symbol: str) -> str`：调用 `GET /internal/monitor/:symbol`
  - `get_alert_history(symbol: str | None = None, days: int = 7) -> str`：调用 `GET /internal/monitor/alerts`
- Modify: `src/aistock_agent/tools/tenx_tools.py`（原占位文件实现）
  - `get_tenx_score(symbol: str) -> str`：调用 `GET /internal/tenx/score/:symbol`
  - `get_tenx_top_stocks(limit: int = 20) -> str`：调用 `GET /internal/tenx/top`
- Create: `src/aistock_agent/tools/graph_tools.py`
  - `get_concepts() -> str`：调用 `GET /internal/graph/concepts`
  - `get_graph_by_concept(concept: str) -> str`：调用 `GET /internal/graph/:concept`
- Create: `src/aistock_agent/tools/hot_burst_tools.py`
  - `get_hot_burst(limit: int = 20) -> str`：调用 `GET /internal/institution-research`
  - `get_hot_burst_history(days: int = 30) -> str`：调用 `GET /internal/institution-research/history`
- Modify: `src/aistock_agent/services/data_client.py`（确认有对应方法，缺失则补）
- Modify: `src/aistock_agent/api/routes.py`（`/skills` 端点注册新 tools）
- Create: `tests/unit/test_graph_tools.py`、`test_hot_burst_tools.py`、`test_monitor_tools.py`、`test_tenx_tools.py`、扩展 `test_sector_tools.py`

**Interfaces:**
- Produces: 9 个新 `@tool` 函数
- Consumes: `services.data_client`、`tools.base.BaseToolMixin`

**验收标准:**
- [ ] `pytest tests/unit/test_*_tools.py -v` 全绿（每个 tool 至少 2 个测试：正常 + 异常降级）
- [ ] 每个 tool 用 `BaseToolMixin` 或 `safe_tool_call`，异常返回降级文本
- [ ] `/skills` 端点返回 14 个 tools（原 9 + 新 5 个文件含 9 个函数）
- [ ] `mypy src/` 无 error

**依赖:** Task 6（接口契约对齐，可并行开发用 mock）

---

## Task 8: Express 反代配置

**目标:** 在 `aistock-app-api` 侧配置 `/api/agent/*` 反代到 Python FastAPI 服务，SSE 流式透传，自动注入 `X-Internal-Token`。

**Files:**
- Modify: `aistock-app-api/src/modules/agent/agent.proxy.ts`（或新建反代模块）
  - 用 `http-proxy-middleware` 或 Express 原生 proxy
  - `/api/agent/*` → `http://python-service:8000/*`（路径重写：去掉 `/api/agent` 前缀）
  - 注入 `X-Internal-Token` header
  - SSE 透传：禁用 buffer，`Accept: text/event-stream` 时设置 `Connection: keep-alive`
- Modify: `aistock-app-api/src/app.ts`（挂载反代中间件）
- Modify: `aistock-app-api/.env.example`（新增 `PYTHON_AGENT_URL=http://localhost:8000`、`INTERNAL_API_TOKEN=xxx`）
- Create: `aistock-app-api/src/modules/agent/__tests__/agent.proxy.spec.ts`（验证路径重写、header 注入、SSE 透传）

**Interfaces:**
- Produces: Express 反代中间件

**验收标准:**
- [ ] `npm test` 全绿，反代测试覆盖路径重写 + header 注入 + SSE 透传
- [ ] 前端调 `/api/agent/briefing/morning` 能收到 SSE 流（手动 curl 验证）
- [ ] 前端调 `/api/agent/chat/message` 能收到 JSON 响应
- [ ] Python 侧日志可见请求到达，且 `X-Internal-Token` 校验通过
- [ ] SSE 响应无 buffer 延迟（实时流式）

**依赖:** Task 6（Node.js 接口就绪）、Task 7（Python tools 就绪，验证全链路）

---

## Task 9: 端到端测试

**目标:** 验证"前端 → Node.js → Python → Node.js internal"全链路跑通，覆盖 5 类意图 + 晨报 + 异常路径。

**Files:**
- Create: `aistock-agent-py/tests/e2e/test_full_flow.py`
  - `test_full_flow_morning`：mock Node.js `/internal/*` 返回预设数据，调用 `/briefing/morning`，验证 SSE 事件序列含 tool_start/tool_end/text/done
  - `test_full_flow_stock`：mock supervisor → stock，mock `/internal/quote` + `/internal/flow`，验证 `/chat/message` 返回分析文本
  - `test_full_flow_sector`、`test_full_flow_event`、`test_full_flow_general`：同上
  - `test_full_flow_tool_failure_degradation`：mock `/internal/quote` 返回 500，验证降级文本
  - `test_full_flow_redis_cache_hit`：mock Redis 缓存命中，验证不调用 LLM
- Modify: `aistock-app-api/src/modules/agent/__tests__/agent.proxy.spec.ts`（补充反代 + Python 联调测试，mock Python 响应）

**Interfaces:**
- Produces: 端到端测试套件

**验收标准:**
- [ ] `pytest tests/e2e/test_full_flow.py -v` 全绿
- [ ] 5 类意图端到端全覆盖
- [ ] 工具失败降级路径验证
- [ ] Redis 缓存命中路径验证
- [ ] Node.js 反代 + Python 联调测试全绿

**依赖:** Task 1-8 全部完成

---

## Task 10: AGENT_STANDARDS.md 开发标准文档

**目标:** 落实 refactor-plan.md 第 11 节大纲，产出 `AGENT_STANDARDS.md`，覆盖 8 个 Agent 开发规范，供团队后续扩展参考。

**Files:**
- Create: `aistock-agent-py/AGENT_STANDARDS.md`
  - **1. State-first 原则**：所有数据通过 AgentState 流转，禁止节点间隐式传递；新增状态字段必须修改 `state/schema.py`
  - **2. 新增 Tool 流程**：命名规范（`get_xxx` / `search_xxx`）/ 参数类型注解 + docstring / 错误处理用 `BaseToolMixin` / pytest mock 要求 / 在 `/skills` 注册
  - **3. 新增 Agent 流程**：放 `agents/workers/<name>.py` / 实现 `async def run(state) -> dict` / 在 `graph/builder.py` 注册节点 / 在 `graph/routers/intent_router.py` 加路由条件 / 在 `services/llm.py` 绑定模型 / 在 `prompts/workers/<name>.py` 放提示词
  - **4. 提示词管理**：统一放 `prompts/` 对应子目录 / 日期等动态内容用 `{{PLACEHOLDER}}` 占位运行时替换 / 禁止代码内硬编码长提示词 / 版本变更加注释
  - **5. 错误处理规范**：Tool 失败抛 `ToolExecutionError` 由 `safe_tool_call` 捕获 / Agent `run()` 顶层 try-catch 返回降级文本 / 禁止异常中断图执行 / 降级文本标注"暂不可用"
  - **6. 双模型使用规则**：`quick_think`（意图分类/兜底/异动识别/业绩预测）/ `deep_think`（晨报/个股/风口/事件/十倍股/播报）/ temperature/max_tokens 从 config 读取
  - **7. 缓存规范**：晨报 Redis TTL=2h / 缓存 key 格式 `<domain>:<sub>:<date>` / 用 `services/cache.py` / 哪类结果应缓存（幂等性分析）
  - **8. 测试覆盖要求**：每个 tool 有 mock 测试（放 `tests/unit/`）/ 每个 Agent 有集成测试（放 `tests/integration/`）/ 路由有端到端测试（放 `tests/e2e/`）/ 测试不依赖真实网络/LLM/Redis
  - **附录：目录结构速查**（贴 Phase 4 重构后的 AFTER 目录树）
  - **附录：常用命令速查**（uvicorn / pytest 分层 / ruff / mypy）
- Modify: `aistock-agent-py/AGENTS.md`（顶部引用 `AGENT_STANDARDS.md`，避免重复）
- Modify: `aistock-agent-py/docs/refactor-plan.md`（第 11 节标记"已补写，见 AGENT_STANDARDS.md"）

**Interfaces:**
- Produces: `AGENT_STANDARDS.md`

**验收标准:**
- [ ] `AGENT_STANDARDS.md` 覆盖 8 个规范章节
- [ ] 每个规范有具体示例（引用现有代码作为正面教材）
- [ ] AGENTS.md 顶部引用 AGENT_STANDARDS.md
- [ ] refactor-plan.md 第 11 节状态更新

**依赖:** Task 1-9 完成（文档需反映实际实现）

---

## Phase 5 整体验收标准

完成以下全部检查后，Phase 5 视为通过：

- [ ] **基础设施**：lifespan 管理 Redis/httpx 全局资源，启动/关闭日志可见
- [ ] **config**：多模型/连接池/超时/日志/LangSmith 参数可配
- [ ] **可观测性**：structlog JSON 日志 + Token 统计 + LangSmith trace（可选）
- [ ] **健康检查**：`/health` + `/health/ready` 可用，依赖状态正确
- [ ] **中间件**：X-Request-ID + 访问日志 + CORS 全部生效
- [ ] **Node.js 接口**：8 个 `/internal/*` 接口可用，有测试
- [ ] **Python Tools**：9 个新 `@tool` 函数实现，有 mock 测试
- [ ] **反代链路**：`/api/agent/*` → Python 透传，SSE 流式无延迟
- [ ] **端到端测试**：5 类意图 + 异常降级 + 缓存命中全链路验证
- [ ] **标准文档**：`AGENT_STANDARDS.md` 覆盖 8 个规范

---

## 风险与决策点

### 风险 1：Node.js Service 未实现或数据不稳定
- **风险**：`WindLeaderService` / `StockMonitorService` 等可能在 Node.js 侧尚未完整实现
- **缓解**：Task 6 前先盘点 `aistock-app-api/src/modules/` 下 Service 实际状态，未实现的标记为 stub 返回固定 JSON，后续补齐
- **决策**：本 Phase 以"接口契约对齐"为验收，Service 内部数据质量由 Node.js 团队负责

### 风险 2：SSE 反代 buffer 问题
- **风险**：Express 默认 buffer 响应，导致 SSE 流式失效
- **缓解**：Task 8 用 `http-proxy-middleware` 时配置 `selfHandleResponse: false` + 禁用 compression，手动 curl 验证流式
- **回滚**：若反代 SSE 失败，前端直连 Python 服务（绕过 Node.js），但破坏统一鉴权

### 风险 3：LangSmith 引入额外延迟
- **风险**：开启 LangSmith tracing 可能增加 LLM 调用延迟
- **缓解**：默认 `LANGSMITH_ENABLED=false`，仅排障时开启
- **决策**：生产环境不开启 LangSmith，用本地 TokenUsageCallback 满足基本可观测性

### 风险 4：Redis 连接池耗尽
- **风险**：高并发下 Redis 连接池 max_connections 不够
- **缓解**：config 可配 `redis_max_connections`，默认 10，生产按负载调整
- **监控**：`/health/ready` 检查 Redis 连通性，连接池耗尽时返回 degraded

### 决策点：是否引入 Prometheus metrics
- **建议**：本 Phase 不引入 Prometheus，`MetricsCollector` 只暴露 `/metrics` JSON 端点。Phase 6+ 若需接入 Grafana 再考虑 prometheus_client。

### 决策点：CORS origins 配置
- **建议**：开发默认 `["*"]`，生产通过 env `CORS_ORIGINS=https://aistock.example.com,https://app.aistock.com` 限定。`.env.example` 给出示例。

### 决策点：Node.js 反代是否需要限流
- **建议**：本 Phase 不做限流（C 类，用户明确不做）。Phase 6+ 接入业务流量后再评估。

---

## 实施顺序建议

按 Task 依赖关系，推荐顺序：

```
Task 1 (lifespan) → Task 2 (config) → Task 3 (health) → Task 4 (observability) → Task 5 (middleware)
                                                                              ↓
Task 6 (Node.js /internal/*) ──┐                                              │
Task 7 (Python Tools)       ───┤ → Task 8 (Express 反代) → Task 9 (端到端) → Task 10 (文档)
                               │
                  （6 和 7 可并行）
```

总工作量预估：10 个 Task，每个 Task 0.5-1.5 天，总计约 7-12 个工作日。

---

## 跨仓库协作注意

本 Phase 涉及 `aistock-app-api` 和 `aistock-agent-py` 两个仓库的协同：

1. **接口契约先行**：Task 6 和 Task 7 可并行，但必须先对齐接口契约（路径/参数/响应格式），建议在 Task 6 Step 1 先产出接口文档（可复用本文档的接口清单表）
2. **跨端同步检查**：按 aistock-workflow rules，修改 Node.js 或 Python 任一侧时，询问"另一端是否需要同步"
3. **分支策略**：两仓库都在 `changer` 分支开发，PR 合并到 `master`（api）或 `main`（agent-py）
4. **端到端联调**：Task 9 需要两仓库同时启动服务，建议用 docker-compose 编排（若已有）

---

*本计划在实施过程中持续更新。架构变更需同步修改本文档及 refactor-plan.md。Phase 4 必须完成后方可进入 Phase 5。*
