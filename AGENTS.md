# AGENTS.md - aistock-agent-py

> 本文档是 AI Agent 的入口地图，开发时 AI 必读。
>
> **新增 Agent / Tool 时必读**：[AGENT_STANDARDS.md](./AGENT_STANDARDS.md) — 覆盖 8 个核心
> 开发规范（State-first / Tool / Agent / 提示词 / 错误处理 / 双模型 / 缓存 / 测试）+ 4 个补充
> 规范（可观测性 / API / 配置 / 代码风格）+ 目录结构与常用命令速查。本文件不重复其内容，
> 仅保留入口地图、目录结构速览与异常降级规范。

## 项目概述

AiStock Agent 推理服务，基于 Python FastAPI + LangGraph，负责多 Agent 编排和深度推理。Node.js 后端（aistock-app-api）负责数据层和 HTTP 接入，本服务专注推理。

## 产品功能 → Agent 映射

| 产品功能 | Agent 文件 | 模型 | 优先级 |
|---------|-----------|------|--------|
| 意图路由 | agents/supervisor/node.py | quick_think | P0 |
| 早点听/晨报 | agents/workers/morning.py | deep_think | P0 |
| 个股分析 | agents/workers/stock.py | deep_think | P0 |
| 板块分析 | agents/workers/sector.py | deep_think | P0 |
| 事件传导链 | agents/workers/event.py | deep_think | P0 |
| 长线风口/风口龙头 | workers/wind_leader.py | deep_think | P0 |
| 异动提醒/持仓监控 | workers/alert.py | deep_think | P1 |
| 机构调研热门股 | workers/hot_burst.py | deep_think | P1 |
| 播报生成 | workers/broadcast.py | deep_think | P0（核心特色） |
| 智能投顾 | workers/ai_advisor.py | deep_think | P0 |
| 交易复盘 | workers/review.py | deep_think | P2 |
| 十倍股评分 | workers/tenx.py（Phase 5+） | deep_think | P2 |
| 趋势股评分 | workers/trend_score.py | deep_think | P2 |
| 业绩预测 | workers/forecast.py（Phase 5+） | quick_think | 后续 |
| 兜底对话 | agents/general/node.py | quick_think | P0 |

## 核心架构

### Graph 拓扑

```
START → supervisor(quick_think, 意图路由)
  ├── intent="morning"       → morning_agent（deep_think）
  ├── intent="stock"         → stock_analyst（deep_think）
  ├── intent="sector"        → sector_analyst（deep_think）
  ├── intent="event"         → event_analyst（deep_think, v3模块化）
  ├── intent="wind_leader"   → wind_leader_agent（deep_think）
  ├── intent="hot_burst"     → hot_burst_agent（deep_think）
  ├── intent="alert"         → alert_agent（deep_think）
  ├── intent="broadcast"     → broadcast_agent（deep_think）
  ├── intent="ai_advisor"    → ai_advisor_agent（deep_think）
  ├── intent="trend_score"   → trend_score_agent（deep_think）
  ├── intent="general"       → general_agent（quick_think）
  └── [user触发, intent≠general/broadcast] → ai_advisor_agent（DB报告→整理回复）
        │
        ▼
       END

定时链路（APScheduler, 非LangGraph图内边）：
  08:50 morning_agent → 提取major_events → 并行event_analyst(fire-and-forget)
  09:00 morning(缓存)→wind_leader→hot_burst→trend_score→broadcast（串行，写DB+双人语音播报, 9:10前端可见）
  15:30 review_agent → 15:35 snapshot_builder → 15:40 iterate_agent（复盘流水线, 文件I/O传递）
```

### 多专家 Agent 协作体系（参考涨乐AI）

```
用户请求（如"早点听"、"异动提醒播报"）
       │
       ▼
   supervisor（意图理解与任务调度）
       │
       ├──→ morning_agent：宏观分析（避险需求、利率、政策等）
       │      └──→ major_events → event_analyst（并行传导分析）
       │
       ├──→ wind_leader_agent：长线风口与龙头筛选
       │
       ├──→ alert_agent：异动识别与风险监控
       │
       ├──→ hot_burst_agent：机构调研共振检测
       │
       ├──→ event_analyst：事件传导链路分析（v3模块化+双层输出）
       │
       ├──→ stock_analyst：个股深度分析
       │
       ├──→ ai_advisor_agent：智能投顾（DB报告→整理回复）
       │
       └──→ broadcast_agent：播报生成
              │
              ├──→ 输出双人对话格式（AI分析师 + AI主持人）
              └──→ Node.js TTS 语音合成 + 前端播报播放
```

### 数据流

- Python 通过 `services/data_client.py`（httpx）回调 Node.js `/internal/*` 获取 A 股数据
- 境外市场数据（yfinance）和全网搜索（Tavily）在 Python 侧直接调用
- **禁止在 Python 重复实现 A 股数据获取逻辑**

### 双模型策略

- `quick_think`（gpt-4o-mini）：意图分类、兜底对话、异动识别、业绩预测
- `deep_think`（gpt-4o）：晨报分析、个股/风口/事件/十倍股/播报深度分析

## 目录结构

> Phase 4 重构后（2026-07-07）。agents/ 物理分层为 supervisor/ + general/ + workers/。

```
src/aistock_agent/
├── main.py              # FastAPI 入口
├── config.py            # pydantic-settings 配置
├── constants.py         # SSE 事件类型 / intent 集合 / 错误码 / TOOL_LABELS
├── state/
│   └── schema.py        # AgentState TypedDict
├── schemas/             # 对外交互 Pydantic 数据模型
│   ├── chat.py          # ChatRequest / ChatResponse
│   ├── sse.py           # SSEEvent
│   └── agents.py        # 各 Agent 输入/输出 schema
├── memory/              # 持久化记忆模块
│   ├── checkpointer.py  # LangGraph checkpointer 工厂（MemorySaver 默认）
│   ├── session_store.py # 会话历史读写
│   └── preferences.py   # 用户偏好/自选股记忆
├── utils/               # 通用工具
│   ├── sse.py           # LangGraph 事件 → SSE 事件映射
│   ├── parser.py        # LLM 输出解析（parse_intent）
│   ├── message.py       # 消息提取（extract_last_human_message / extract_final_ai_response）
│   ├── output_parser.py # Event Agent 双层输出解析（display_report + podcast_brief）
│   └── date.py          # 日期/交易日工具
├── errors/              # 异常体系
│   └── exceptions.py    # AgentError / DataUnavailableError / LLMTimeoutError / ToolExecutionError / RouteError
├── graph/
│   ├── builder.py       # StateGraph 构建 + compile()（哨兵模式挂载 checkpointer）
│   └── routers/
│       └── intent_router.py  # route_by_intent（从 edges.py 迁入）
├── agents/              # 物理分层：supervisor/ + general/ + workers/
│   ├── supervisor/
│   │   └── node.py      # 意图分类（quick_think）
│   ├── general/
│   │   └── node.py      # 兜底对话（quick_think）
│   └── workers/
│       ├── morning.py   # 晨报（ReAct + Redis 缓存）
│       ├── stock.py     # 个股分析
│       ├── sector.py    # 板块分析
│       └── event.py     # 事件传导链（v2：Redis 缓存 + 双层输出解析 + 持久化）
├── tools/
│   ├── base.py          # safe_tool_call 装饰器 + BaseToolMixin + DEGRADED_MESSAGE
│   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
│   ├── sector_tools.py  # get_leader_stocks, get_wind_leaders
│   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
│   ├── market_tools.py  # get_global_markets, tavily_finance_search
│   ├── monitor_tools.py # get_stock_monitor, get_alert_history（Phase 5）
│   ├── tenx_tools.py    # get_tenx_score, get_tenx_top_stocks（Phase 5）
│   ├── trend_tools.py   # get_trend_score, get_trend_score_detail, get_trend_top_stocks
│   ├── graph_tools.py   # get_concepts, get_graph_by_concept（Phase 5）
│   └── hot_burst_tools.py # get_hot_burst, get_hot_burst_history（Phase 5）
├── prompts/             # 分层对应 agents 目录
│   ├── supervisor/routing.py
│   ├── general/system.py
│   └── workers/{morning,stock,sector,event,wind_leader,hot_burst,broadcast,ai_advisor,trend_score,alert,review,iterate}.py
├── services/
│   ├── llm.py           # 双模型工厂（从 agents/base.py 迁移）
│   ├── data_client.py   # httpx → Node.js /internal/* API（get / get_list）
│   ├── redis_pool.py    # Redis 连接池单例（lifespan 管理）
│   ├── http_client.py   # httpx AsyncClient 连接池单例（lifespan 管理）
│   └── cache.py         # 晨报缓存服务（基于 RedisPool）
├── observability/       # 可观测性包（Phase 5）
│   ├── logging.py       # structlog JSON 日志配置（setup_logging / get_logger）
│   ├── metrics.py       # MetricsCollector 线程安全计数器（token/call/error）
│   └── callback.py      # LangChain 回调（TokenUsage / AgentTrace，零侵入业务逻辑）
└── api/
    ├── routes.py        # REST 接口（/chat/message + /chat/stream SSE + /briefing/morning + /skills + /health + /health/ready）
    ├── deps.py          # 依赖注入（verify_internal_token / build_initial_state）
    ├── middleware.py    # HTTP 中间件（request_id 注入、访问日志、CORS）（Phase 5）
    └── ws.py            # WebSocket 流式接口（astream_events v2，7 种事件类型 + 节点标签映射）
```

## 开发规范

> 完整规范见 [AGENT_STANDARDS.md](./AGENT_STANDARDS.md)。以下为速览，新增 Agent / Tool
> 时以 AGENT_STANDARDS.md 为准。

### State-first 原则
- 所有数据通过 AgentState 流转，禁止节点间隐式传递
- 新增状态字段必须修改 `state/schema.py`

### 新增 Tool 流程
1. 在 `tools/` 对应文件中定义 `@tool` + `@safe_tool_call` 装饰的 async 函数
2. 参数必须定义类型注解和 docstring（供 LLM 理解工具用途）
3. 通过 `services/data_client.py` 的 `NodeApiClient` 调用 Node.js `/internal/*` 接口
4. 在 `api/routes.py` 的 `all_tools` 列表中注册
5. 必须编写 mock 测试（正常 + 异常降级，`tests/unit/` 目录）

### 新增 Agent 流程
1. 在 `agents/workers/` 新增文件，实现 `async def run(state: AgentState) -> dict`
2. 在 `graph/builder.py` 注册节点
3. 在 `graph/routers/intent_router.py` 添加路由条件（如果需要新 intent）
4. 在 `services/llm.py` 绑定对应的工具集（quick_think / deep_think）

### 提示词管理
- 统一存放 `prompts/` 目录
- 日期等动态内容用占位符（如 `{{DATE}}`），运行时替换
- 不在代码中硬编码长提示词

### 错误处理
- Tool 失败时返回降级文本（如"数据暂不可用"），不抛异常中断图执行
- Agent 节点必须 try-catch 包裹

### 缓存规范
- 晨报结果缓存 Redis TTL=2小时
- 缓存 key 格式：`briefing:morning:YYYY-MM-DD`

## Node.js 侧配合接口

Python 服务通过以下接口获取 A 股数据（需携带 `X-Internal-Token`）：

| 接口 | 数据源 | 说明 |
|------|--------|------|
| `GET /internal/quote/:symbol` | 腾讯行情 | 个股实时行情 |
| `GET /internal/flow/:symbol` | 新浪+Tushare | 资金流向 |
| `GET /internal/leader/:tagCode` | Tushare | 板块龙头 |
| `GET /internal/news/search/:symbol` | 财联社 | 个股新闻 |
| `GET /internal/news/fulltext/:id` | 财联社 | 新闻全文 |
| `GET /internal/forecast/:symbol` | 同花顺 | 盈利预测 |
| `GET /internal/wind-leaders` | 风口算法 | 风口龙头数据 |
| `GET /internal/monitor/:symbol` | 异动引擎 | 个股异动数据 |
| `GET /internal/tenx/score/:symbol` | 十倍股评分 | 评分详情 |
| `GET /internal/tenx/top` | 十倍股评分 | 排行列表 |
| `GET /internal/trend/score/:symbol` | 趋势股评分 | 评分详情（4维度） |
| `GET /internal/trend/score/:symbol/detail` | 趋势股评分 | 评分展开详情（含K线、新闻等） |
| `GET /internal/trend/top` | 趋势股评分 | 排行列表 |
| `GET /internal/graph/concepts` | 知识图谱 | 产业链概念列表 |
| `GET /internal/graph/:concept` | 知识图谱 | 产业链图谱数据 |
| `GET /internal/institution-research` | 机构调研热门股 | 共振检测结果 |
| `GET /internal/institution-research/history` | 机构调研热门股 | 历史记录 |
| `POST /internal/briefing/generate-audio` | 火山引擎/Azure TTS | 根据 broadcast 报告生成音频并写回 audio_path |

## 常用命令

```bash
uvicorn aistock_agent.main:app --reload   # 开发模式
pytest tests/ -v                           # 运行全部测试
pytest tests/unit/ -v                      # 仅单元测试（工具函数）
pytest tests/integration/ -v               # 仅集成测试（Agent + Graph）
pytest tests/e2e/ -v                       # 仅端到端测试（HTTP 接口）
$env:PYTHONPATH = "src"; python scripts/run_morning_test.py  # 晨报生成并落盘到 docs/agent-outputs/morning/
ruff check src/                            # 代码检查
mypy src/                                  # 类型检查
python -c "from aistock_agent.graph.builder import compile_graph; compile_graph()"  # 验证图可编译
```

## 关键约束

- 禁止在 Python 重复实现 A 股数据获取逻辑
- **agents 物理分层**：`supervisor/` + `general/` + `workers/`，禁止混放（Phase 4 落地）
- **各 agent run() 必须有顶层 try-catch**，返回降级文本不抛异常（见"异常降级规范"，Phase 4 落地）
- **compile_graph() 默认挂 checkpointer**，graph.ainvoke/astream 必须传 `config={"configurable": {"thread_id": ...}}`（Phase 5 落地，不传会抛 ValueError）
- **工具用 @safe_tool_call 装饰器**，返回降级文本不抛异常（Phase 4 落地）
- LLM 调用失败时返回降级文本，不重试
- yfinance 仅用于境外市场数据（美股/亚太/大宗/汇率）
- 晨报 Agent 必须通过 Redis 缓存，同一天不重复调用 deep_think
- 播报 Agent 是核心特色，所有分析 Agent 都需对接播报输出

## 异常降级规范（Phase 4 落地）

### 两层降级体系

1. **工具层**（`tools/base.py` 的 `@safe_tool_call` 装饰器）：
   - 捕获工具异常 → structlog 记录 → 返回 `DEGRADED_MESSAGE = "数据暂不可用，请稍后重试"`
   - LLM 会看到降级文本作为 observation，按 prompts 要求在最终回复中标注"数据暂不可用"
   - 不抛异常，graph 继续执行

2. **Agent 层**（各 `run()` 的顶层 try-catch）：
   - 捕获 LLM/Graph 框架异常（`get_deep_think()` 失败、`create_react_agent()` 失败、`ainvoke()` 失败）
   - structlog 记录 → 返回符合 AGENTS.md 规范的降级文本（标注"暂不可用"，不猜测数据）
   - 不抛异常，graph 不中断

### 降级文本（每个 agent 不同，便于日志区分）

| Agent | 降级文本 |
|-------|---------|
| supervisor | `{"intent": "general"}`（路由降级到 general 兜底） |
| morning | `{"final_response": "晨报生成暂时不可用，请稍后重试"}` |
| stock | `{"final_response": "个股分析暂时不可用，请稍后重试"}` |
| sector | `{"final_response": "板块分析暂时不可用，请稍后重试"}` |
| wind_leader | `{"final_response": "长线风口分析暂时不可用，请稍后重试"}` |
| hot_burst | `{"final_response": "机构调研热门股分析暂时不可用，请稍后重试"}` |
| event | `{"final_response": "事件分析暂时不可用，请稍后重试"}` |
| review | `{"final_response": "复盘生成暂时不可用，请稍后重试"}` |
| alert | `{"final_response": "异动提醒暂时不可用，请稍后重试"}` |
| ai_advisor | `{"final_response": "智能投顾暂时不可用，请稍后重试"}` |
| broadcast | `{"final_response": "播报生成暂时不可用，请稍后重试"}` |
| trend_score | `{"final_response": "趋势股评分分析暂时不可用，请稍后重试"}` |
| general | `{"final_response": "抱歉，我暂时无法处理您的请求，请稍后重试"}` |

### 不做异常分类 catch

只 catch `Exception` 一层，不写 `except ToolExecutionError` / `except LLMTimeoutError`（当前无抛出点，是 dead code）。未来有显式抛出场景再补分类。

### 报告双层输出（schema_version 2.0）

所有 Agent 持久化到数据库的 `content` 字段采用双层结构。

**为什么要做双层输出？（两个核心原因）**

1. **前端展示需要**：前端页面需要"概要 + 完整报告内容"两层数据。`display_report.summary` 用于列表页/卡片快速浏览，`display_report.details` 用于详情页完整展示。单层 text 无法支撑结构化展示。
2. **省 token（核心动机）**：双人播报语音生成费用较高，不能把完整长报告（500-1500字）喂给播报模型。`podcast_brief` 作为 broadcast_agent 和 ai_advisor_agent 的原材料，只输入 150-200 字的摘要，大幅降低 token 消耗。如果喂整个报告，token 成本会高数倍且播报模型容易跑偏。

双层结构定义如下：

```python
content = {
    "display_report": {
        "summary": "结论一句话（20字以内）",
        "details": "完整分析内容（500-1500字）",
        "stocks": ["股票代码"],   # 可选
        "risks": ["风险提示"]      # 可选
    },
    "podcast_brief": "150-200字的播报摘要，只含主题、事实、判断、风险",
    "schema_version": "2.0"
}
```

**消费方**：
- `broadcast_agent`：读取 `podcast_brief`（通过 `extract_podcast_brief`），汇总生成双人对话
- `ai_advisor_agent`：读取 `display_report`（通过 `extract_display_report`），整理成对话回复

**兼容性**：`utils/report_parser.py` 自动兼容 1.0 单层 `{"text": "..."}` 和 2.0 双层结构，旧报告无需迁移。

**LLM 输出要求**：Agent 提示词中须明确要求 LLM 在最终回复中输出 JSON 格式的双层内容。`parse_dual_layer_response` 函数会解析 JSON，解析失败时降级为单层（display_report.details = 原文本）。

**已改造 Agent**：wind_leader（尹辰）、broadcast（尹辰）、ai_advisor（尹辰）
**待改造 Agent**：morning（王昌泽）、hot_burst（吴涵晶）、alert（李俊良）

