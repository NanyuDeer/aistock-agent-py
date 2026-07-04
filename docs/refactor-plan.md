# AiStock Agent — Python/LangGraph 重构设计文档

> 版本：v1.0 | 日期：2026-07-04 | 状态：规划确认

---

## 1. 背景与目标

### 现状

`aistock-app-api` 的 `src/modules/agent` 模块使用 TypeScript/Express 实现，架构为：

```
orchestrator.ts → AgentRegistry(LLM路由) → Agent.handle() → Skill.execute() → 数据Service → 返回
```

目前已实现：`general_agent`（兜底）+ 3个Skill（个股行情/资金流向/龙头股）。

晨报/事件分析/持仓守护等Agent均为"开发中"状态，提示词已写但未实现。

### 重构目标

1. 将 Agent 推理层迁移至 Python，使用 **LangGraph** 构建多Agent状态机
2. 支持**晨报Agent**（预市宏观分析，最高优先级）
3. 支持**个股/板块/事件**分析对话
4. 保持 Node.js 作为数据层和 HTTP 接入层，Python 服务专注推理
5. 重构后输出 Agent 开发标准文档

---

## 2. 架构决策

### 2.1 服务边界（已确认：方案A）

```
┌─────────────────────────────────────────┐
│  Node.js Express API（保留）             │
│  · HTTP路由 / WebSocket接入              │
│  · 用户认证 / 会话管理                   │
│  · 全部A股数据Service（Tencent/Sina/THS）│
│  · /api/agent/* → 反代到Python服务       │
│  · /internal/* → Python专用数据接口      │
└──────────────┬──────────────────────────┘
               │ HTTP + SSE
┌──────────────▼──────────────────────────┐
│  Python FastAPI Agent服务（新建）        │
│  · LangGraph图编排（纯推理层）           │
│  · 通过 /internal/* 回调Node.js拿数据    │
│  · yfinance：境外指数/大宗/汇率          │
│  · Tavily：全网财经新闻搜索              │
│  · Redis checkpointer：会话持久化        │
└─────────────────────────────────────────┘
```

**原则：Python服务不拥有数据，只拥有推理。A股实时数据留Node.js。**

例外：`yfinance`（境外市场）和 `Tavily`（全网搜索）在Python侧直接调用，Node.js无对应实现。

### 2.2 部署方式（已确认）

- **HTTP微服务**，Python独立进程
- 项目已部署服务器测试，两个服务通过docker-compose编排
- Node.js端口：待确认；Python端口：待确认

### 2.3 双模型策略（参考TradingAgents）

| 用途 | 模型 | 原因 |
|------|------|------|
| 意图分类/路由 | `quick_think`（gpt-4o-mini） | 低延迟，成本低 |
| 深度分析/晨报/事件 | `deep_think`（gpt-4o / claude-opus-4-8） | 推理质量优先 |

---

## 3. 技术栈与依赖

### pyproject.toml

```toml
[project]
name = "aistock-agent"
version = "1.0.0"
requires-python = ">=3.11"

dependencies = [
    # LangGraph 核心
    "langgraph==0.2.74",
    "langchain-core==0.3.58",
    "langchain-openai==0.2.14",

    # FastAPI 服务层
    "fastapi==0.115.12",
    "uvicorn[standard]==0.34.2",
    "sse-starlette==2.2.1",

    # 数据获取（Python侧直接调用）
    "yfinance==0.2.54",        # 境外市场数据（美股/亚太/大宗/汇率）
    "tavily-python==0.5.0",    # 全网财经新闻搜索

    # 数据调用 & 配置
    "httpx==0.28.1",            # 异步HTTP → Node.js /internal/* API
    "redis==5.2.1",             # 会话持久化 / 晨报缓存
    "pydantic==2.11.5",
    "pydantic-settings==2.8.1",
    "python-dotenv==1.1.0",
    "structlog==25.3.0",
]

[project.optional-dependencies]
dev = [
    "pytest==8.3.5",
    "pytest-asyncio==0.25.3",
    "pytest-mock==3.14.0",
    "mypy==1.15.0",
    "ruff==0.11.12",
]
```

---

## 4. 目录结构

```
aistock-agent-py/
├── pyproject.toml
├── .env.example
├── Dockerfile
├── AGENT_STANDARDS.md          # Phase 5 完成后补写
├── docs/
│   └── refactor-plan.md        # 本文档
│
└── src/
    └── aistock_agent/
        ├── __init__.py
        ├── main.py              # FastAPI app入口
        ├── config.py            # pydantic-settings，读取环境变量
        │
        ├── state/               # 独立状态层（PrimoAgent模式）
        │   └── schema.py        # AgentState TypedDict
        │
        ├── graph/               # 图拓扑层（只管骨架）
        │   ├── edges.py         # 所有条件边函数
        │   └── builder.py       # StateGraph构建 + compile()
        │
        ├── agents/              # 每个Agent一个文件，含节点函数
        │   ├── base.py          # 双模型工厂（quick_think/deep_think）
        │   ├── supervisor.py    # 意图分类节点（quick_think）
        │   ├── morning_agent.py # 晨报节点（ReAct + deep_think）★优先
        │   ├── stock_analyst.py # 个股分析节点
        │   ├── sector_analyst.py# 板块分析节点
        │   ├── event_analyst.py # 事件传导链节点
        │   └── general_agent.py # 兜底节点
        │
        ├── tools/               # LangChain @tool，按数据域分组
        │   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
        │   ├── sector_tools.py  # get_leader_stocks
        │   ├── news_tools.py    # search_cls_news, get_news_fulltext
        │   └── market_tools.py  # get_global_markets（yfinance）, tavily_finance_search
        │
        ├── prompts/             # 所有提示词集中管理
        │   ├── morning.py       # 晨报宏观分析提示词（4步框架）
        │   ├── routing.py       # 路由分类提示词
        │   └── system.py        # 通用系统提示词
        │
        ├── services/
        │   └── data_client.py   # httpx AsyncClient → Node.js /internal/* API
        │
        └── api/
            ├── routes.py        # REST接口
            └── ws.py            # WebSocket流式接口
```

---

## 5. State Schema

```python
# state/schema.py
from typing import Annotated, Optional, Any
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    # 对话历史（add_messages reducer，追加不覆盖）
    messages: Annotated[list, add_messages]
    # 会话元数据
    session_id: str
    user_id: Optional[str]
    favorites: list[str]           # 用户自选股列表
    # 路由信息（supervisor写入）
    intent: Optional[str]          # stock | sector | event | morning | general
    symbol: Optional[str]          # 提取的股票代码
    tag_code: Optional[str]        # 提取的板块代码
    # 分析报告累积（多步分析时复用，参考TradingAgents）
    analysis_reports: dict[str, str]
    # 最终响应
    final_response: Optional[str]
```

---

## 6. Graph 设计

```
START
  │
  ▼
supervisor（quick_think）
  │ 写入 state.intent
  │
  ├─── intent="morning"  ──▶  morning_agent   ★ 最高优先
  ├─── intent="stock"    ──▶  stock_analyst
  ├─── intent="sector"   ──▶  sector_analyst
  ├─── intent="event"    ──▶  event_analyst
  └─── intent="general"  ──▶  general_agent
                │
                ▼
               END
```

### 条件边（graph/edges.py）

```python
def route_by_intent(state: AgentState) -> str:
    intent = state.get("intent", "general")
    valid = {"morning", "stock", "sector", "event", "general"}
    return intent if intent in valid else "general"
```

---

## 7. Agent 详细设计

### 7.1 Supervisor Agent

- **模型**：quick_think（gpt-4o-mini）
- **职责**：意图分类，写入 `state.intent` 和 `state.symbol` / `state.tag_code`
- **不调用任何工具**，纯LLM分类

### 7.2 Morning Agent（晨报）★

- **模型**：deep_think（gpt-4o / claude-opus-4-8）
- **模式**：`create_react_agent`，LLM自主决定搜索策略
- **工具集**：
  - `tavily_finance_search(query: str)` — 宏观事件/政策/经济数据搜索
  - `get_global_markets()` — 美股三大指数/中概ETF/大宗/汇率/亚太指数
  - `get_cls_news(limit: int)` — 财联社国内资讯补充
- **系统提示词**：4步宏观策略分析框架（见 `prompts/morning.py`）
- **日期注入**：运行时动态替换提示词中的日期占位符
- **Token估算**：6000–8000 tokens/次（deep_think模型）
- **缓存**：Redis TTL=2小时，同一天重复调用直接返回缓存
- **前置检查**：判断当日是否为A股交易日，非交易日拒绝执行

**外盘数据覆盖（yfinance）：**

| 类别 | Ticker |
|------|--------|
| 美股 | ^GSPC（S&P500）、^IXIC（纳指）、^DJI（道指）|
| 中概 | KWEB（中概ETF）|
| 亚太 | ^N225（日经）、^HSI（恒生）、^KS11（韩综）|
| 大宗 | GC=F（黄金）、CL=F（原油）|
| 汇率 | USDCNY=X（美元/人民币）|

### 7.3 Stock Analyst Agent

- **模型**：deep_think
- **工具集**：`get_quote` + `get_capital_flow` + `get_profit_forecast` + `search_cls_news`
- **能力**：个股行情 + 资金流向 + 机构预测 + 相关新闻综合分析

### 7.4 Sector Analyst Agent

- **模型**：deep_think
- **工具集**：`get_leader_stocks` + `get_capital_flow`
- **能力**：板块龙头筛选 + 板块资金动向分析

### 7.5 Event Analyst Agent

- **模型**：deep_think
- **工具集**：`search_cls_news` + `get_news_fulltext` + `get_quote` + `tavily_finance_search`
- **能力**：事件传导链分析（事件→行业→个股）

### 7.6 General Agent（兜底）

- **模型**：quick_think
- **工具集**：`get_quote`（基础行情）
- **能力**：关键词触发基础查询，兜底未匹配意图

---

## 8. Node.js 侧配合工作

Python服务通过以下内部接口获取A股数据，需在Node.js侧新增：

```
GET /internal/quote/:symbol          → TencentQuoteService.getQuote()
GET /internal/flow/:symbol           → SinaMoneyFlowService.getSinaMoneyflow()
GET /internal/leader/:tagCode        → TushareTagLeaderService.getLeaderStocks()
GET /internal/news/search/:symbol    → ClsStockNewsService.getStockNews()
GET /internal/news/fulltext/:id      → ClsStockNewsService.getNewsFulltext()
GET /internal/forecast/:symbol       → ThsService.getProfitForecast()
```

这些接口仅供Python服务内部调用，不对外暴露，建议加内网鉴权（IP白名单或固定Header）。

---

## 9. API 接口设计（Python侧）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/chat/message` | 对话消息（非流式） |
| POST | `/chat/stream` | 对话消息（SSE流式） |
| GET | `/briefing/morning` | 晨报（SSE流式，支持Redis缓存）|
| GET | `/skills` | 已注册工具列表 |
| GET | `/health` | 健康检查 |

Node.js侧将 `/api/agent/*` 的请求反代到Python服务对应路径。

---

## 10. 分阶段实施计划

| Phase | 内容 | 核心产出 | 验收标准 |
|-------|------|----------|----------|
| **1** | 项目骨架 | pyproject.toml / config / AgentState / FastAPI `/health` | `uvicorn`启动，`/health` 返回200 |
| **2** | Node.js内部API + Python Tools层 | 6个`/internal/*`接口 + 5个`@tool` | 每个tool有pytest（mock data），独立可调用 |
| **3** | Morning Agent | `agents/morning_agent.py` + Redis缓存 + SSE接口 | `/briefing/morning`返回完整4步分析 |
| **4** | 对话Agent层 | supervisor + stock/sector/event/general agent + graph builder | 完整消息流程：输入→路由→工具调用→回复 |
| **5** | Node.js接入 + 标准文档 | Express反代 + `AGENT_STANDARDS.md` | 端到端测试通过，文档覆盖所有扩展场景 |

---

## 11. Agent 开发标准（大纲，Phase 5补写）

`AGENT_STANDARDS.md` 将覆盖以下内容：

1. **State-first原则**：所有数据通过AgentState流转，禁止节点间隐式传递
2. **新增Tool流程**：命名规范 / 参数校验 / 错误处理 / pytest要求
3. **新增Agent流程**：注册到graph/edges.py / 路由条件声明 / 工具绑定规则
4. **提示词管理**：统一存放`prompts/` / 版本注释 / 日期等动态内容注入规范
5. **错误处理规范**：工具失败降级策略 / 不允许抛异常中断图执行
6. **双模型使用规则**：何时用deep_think，何时用quick_think
7. **缓存规范**：哪类结果应缓存，TTL设置原则
8. **测试覆盖要求**：每个tool必须有mock测试，每个Agent有集成测试

---

## 12. 待确认事项

- [ ] Python服务端口（Node.js当前端口？）
- [ ] Redis地址（与Node.js共用还是独立实例？）
- [ ] Tavily API Key申请
- [ ] 服务器Python环境（3.11+）

---

*本文档在实施过程中持续更新。架构变更需同步修改本文档。*
