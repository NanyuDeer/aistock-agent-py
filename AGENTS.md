# AGENTS.md - aistock-agent-py

> 本文档是 AI Agent 的入口地图，开发时 AI 必读。

## 项目概述

AiStock Agent 推理服务，基于 Python FastAPI + LangGraph，负责多 Agent 编排和深度推理。Node.js 后端（aistock-app-api）负责数据层和 HTTP 接入，本服务专注推理。

## 产品功能 → Agent 映射

| 产品功能 | Agent 文件 | 模型 | 优先级 |
|---------|-----------|------|--------|
| 意图路由 | supervisor.py | quick_think | P0 |
| 早点听/晨报 | morning_agent.py | deep_think | P0 |
| 个股分析 | stock_analyst.py | deep_think | P0 |
| 长线风口/风口龙头 | wind_leader_agent.py | deep_think | P0 |
| 事件传导链 | event_chain_agent.py | deep_think | P0 |
| 异动提醒/持仓监控 | alert_agent.py | quick_think | P1 |
| 机构调研热门股 | hot_burst_agent.py | deep_think | P1 |
| 十倍股/趋势股评分 | tenx_agent.py | deep_think | P2 |
| 业绩预测 | forecast_agent.py | quick_think | 后续 |
| 交易复盘 | review_agent.py | deep_think | P2 |
| **播报生成** | **broadcast_agent.py** | **deep_think** | **P0（核心特色）** |
| 兜底对话 | general_agent.py | quick_think | P0 |

## 核心架构

### Graph 拓扑

```
START → supervisor(quick_think)
  ├── intent="morning"       → morning_agent
  ├── intent="stock"         → stock_analyst
  ├── intent="wind_leader"   → wind_leader_agent
  ├── intent="event_chain"   → event_chain_agent
  ├── intent="alert"         → alert_agent
  ├── intent="hot_burst"     → hot_burst_agent
  ├── intent="tenx"          → tenx_agent
  ├── intent="forecast"      → forecast_agent
  ├── intent="review"        → review_agent
  └── intent="general"       → general_agent
        │
        ▼
  broadcast_agent（播报生成）
        │
        ▼
       END
```

### 多专家 Agent 协作体系（参考涨乐AI）

```
用户请求（如"早点听"、"异动提醒播报"）
       │
       ▼
   supervisor（意图理解与任务调度）
       │
       ├──→ morning_agent：宏观分析（避险需求、利率、政策等）
       │
       ├──→ wind_leader_agent：长线风口与龙头筛选
       │
       ├──→ alert_agent：异动识别与风险监控
       │
       ├──→ hot_burst_agent：机构调研共振检测
       │
       ├──→ event_chain_agent：事件传导链路分析
       │
       ├──→ tenx_agent：十倍股评分与趋势判断
       │
       ├──→ forecast_agent：业绩预测与机构预期
       │
       └──→ stock_analyst：个股深度分析
              │
              ▼
       broadcast_agent（多 Agent 结果汇聚 → 播报生成）
              │
              ├──→ 输出双人对话格式（AI分析师 + AI主持人）
              └──→ 前端 TTS 语音合成 + 播报播放
```

### 数据流

- Python 通过 `services/data_client.py`（httpx）回调 Node.js `/internal/*` 获取 A 股数据
- 境外市场数据（yfinance）和全网搜索（Tavily）在 Python 侧直接调用
- **禁止在 Python 重复实现 A 股数据获取逻辑**

### 双模型策略

- `quick_think`（gpt-4o-mini）：意图分类、兜底对话、异动识别、业绩预测
- `deep_think`（gpt-4o）：晨报分析、个股/风口/事件/十倍股/播报深度分析

## 目录结构

```
src/aistock_agent/
├── main.py              # FastAPI 入口
├── config.py            # pydantic-settings 配置
├── state/
│   └── schema.py        # AgentState TypedDict
├── graph/
│   ├── edges.py         # 条件边（route_by_intent）
│   └── builder.py       # StateGraph 构建 + compile()
├── agents/
│   ├── base.py          # 双模型工厂
│   ├── supervisor.py    # 意图分类
│   ├── morning_agent.py # 晨报（ReAct + Redis 缓存）
│   ├── stock_analyst.py # 个股分析
│   ├── wind_leader_agent.py  # 长线风口
│   ├── event_chain_agent.py  # 事件传导链
│   ├── alert_agent.py        # 异动提醒
│   ├── hot_burst_agent.py    # 机构调研热门股
│   ├── tenx_agent.py         # 十倍股评分
│   ├── forecast_agent.py     # 业绩预测（后续）
│   ├── review_agent.py       # 交易复盘（P2）
│   ├── broadcast_agent.py    # 播报生成（核心特色）
│   └── general_agent.py      # 兜底
├── tools/
│   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
│   ├── sector_tools.py  # get_leader_stocks, get_wind_leaders
│   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
│   ├── market_tools.py  # get_global_markets, tavily_finance_search
│   ├── monitor_tools.py # get_stock_monitor, get_alert_history
│   └── tenx_tools.py    # get_tenx_score, get_tenx_top_stocks
├── prompts/
│   ├── morning.py       # 晨报4步框架
│   ├── event_chain.py   # 事件传导链分析
│   ├── tenx.py          # 十倍股评分
│   ├── broadcast.py     # 双人播报提示词
│   ├── routing.py       # 路由分类
│   └── system.py        # 通用系统提示词
├── services/
│   └── data_client.py   # httpx → Node.js /internal/* API
└── api/
    ├── routes.py        # REST 接口
    └── ws.py            # WebSocket 流式接口
```

## 开发规范

### State-first 原则
- 所有数据通过 AgentState 流转，禁止节点间隐式传递
- 新增状态字段必须修改 `state/schema.py`

### 新增 Tool 流程
1. 在 `tools/` 对应文件中定义 `@tool` 函数
2. 参数必须定义类型注解和 docstring
3. 在 `api/routes.py` 的 `list_skills` 中注册
4. 必须编写 mock 测试（`tests/` 目录）

### 新增 Agent 流程
1. 在 `agents/` 新增文件，实现 `async def run(state: AgentState) -> dict`
2. 在 `graph/builder.py` 注册节点
3. 在 `graph/edges.py` 添加路由条件（如果需要新 intent）
4. 在 `agents/base.py` 绑定对应的工具集

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
| `GET /internal/graph/concepts` | 知识图谱 | 产业链概念列表 |
| `GET /internal/graph/:concept` | 知识图谱 | 产业链图谱数据 |
| `GET /internal/institution-research` | 机构调研热门股 | 共振检测结果 |
| `GET /internal/institution-research/history` | 机构调研热门股 | 历史记录 |

## 常用命令

```bash
uvicorn aistock_agent.main:app --reload  # 开发模式
pytest tests/ -v                          # 运行测试
ruff check src/                           # 代码检查
mypy src/                                 # 类型检查
```

## 关键约束

- 禁止在 Python 重复实现 A 股数据获取逻辑
- LLM 调用失败时返回降级文本，不重试
- yfinance 仅用于境外市场数据（美股/亚太/大宗/汇率）
- 晨报 Agent 必须通过 Redis 缓存，同一天不重复调用 deep_think
- 播报 Agent 是核心特色，所有分析 Agent 都需对接播报输出