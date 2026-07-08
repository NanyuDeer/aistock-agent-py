# AiStock Agent — Python/LangGraph 重构设计文档

> 版本：v2.1 | 日期：2026-07-07 | 状态：Phase 4 完成

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
2. **产品功能驱动**：Agent 命名和职责对齐 App 产品功能（晨报/风口/事件传导/异动/十倍股/业绩预测/复盘），而非技术分类
3. 支持**晨报Agent**（预市宏观分析，最高优先级）
4. 支持**个股/风口/事件传导**分析对话
5. 支持**异动提醒/十倍股评分/业绩预测**等产品功能
6. 保持 Node.js 作为数据层和 HTTP 接入层，Python 服务专注推理
7. 重构后输出 Agent 开发标准文档

### 产品功能 → Agent 映射

| 产品功能 | 对应 Agent | 网页端已有功能 | 涨乐AI参考 |
|---------|-----------|-------------|-----------|
| 早点听/晨报 | morning_agent | 无 | 早点听/盘前播报 |
| 个股分析 | stock_analyst | StockCardList + 详情页 | AI对话 |
| 长线风口/风口龙头 | wind_leader_agent | WindLeaderPanel | 涨停猎手/热点捕手 |
| 事件传导链 | event_chain_agent | AiGraph（AI产业链图谱）| 事件捕手/事件传导 |
| 异动提醒/持仓监控 | alert_agent | StockMonitorView | 特别提醒/持仓陪伴 |
| 机构调研热门股 | hot_burst_agent | HotBurstView | - |
| 十倍股/趋势股评分 | tenx_agent | TenxScoreView | - |
| 业绩预测 | forecast_agent | PerformanceForecastView | 研报精读/业绩解读 |
| 交易复盘 | review_agent | 无 | 交易复盘 |
| **播报生成** | **broadcast_agent** | 无 | **双人播报（AI分析师+AI主持人）** |
| 兜底对话 | general_agent | 无 | 任务助手 |

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
        ├── constants.py         # SSE 事件类型 / intent 集合 / 错误码 / TOOL_LABELS（Phase 4 新增）
        │
        ├── state/               # 独立状态层
        │   └── schema.py        # AgentState TypedDict
        │
        ├── schemas/             # 对外交互 Pydantic 数据模型（Phase 4 新增）
        │   ├── chat.py          # ChatRequest / ChatResponse
        │   ├── sse.py           # SSEEvent
        │   └── agents.py        # 各 Agent 输入/输出 schema
        │
        ├── memory/              # 持久化记忆模块（Phase 4 新增）
        │   ├── checkpointer.py  # LangGraph checkpointer 工厂（MemorySaver 默认，Sqlite/Redis 可选）
        │   ├── session_store.py # 会话历史读写
        │   └── preferences.py   # 用户偏好/自选股记忆
        │
        ├── utils/               # 通用工具（Phase 4 新增）
        │   ├── sse.py           # LangGraph 事件 → SSE 事件映射
        │   ├── parser.py        # LLM 输出解析（parse_intent）
        │   ├── message.py       # 消息提取（extract_last_human_message / extract_final_ai_response）
        │   └── date.py          # 日期/交易日工具
        │
        ├── errors/              # 异常体系（Phase 4 新增）
        │   └── exceptions.py    # AgentError / DataUnavailableError / LLMTimeoutError / ToolExecutionError / RouteError
        │
        ├── graph/               # 图拓扑层
        │   ├── builder.py       # StateGraph 构建 + compile()（哨兵模式挂载 checkpointer）
        │   └── routers/         # 条件边路由函数集中（Phase 4 新增）
        │       └── intent_router.py  # route_by_intent
        │
        ├── agents/              # Agent 节点（Phase 4 物理分层：supervisor/ + general/ + workers/）
        │   ├── supervisor/      # 路由决策节点
        │   │   └── node.py      # 从 agents/supervisor.py 迁入
        │   ├── general/         # 兜底通用节点
        │   │   └── node.py      # 从 agents/general_agent.py 迁入
        │   └── workers/         # 深度业务专业智能体
        │       ├── morning.py   # 从 morning_agent.py 迁入
        │       ├── stock.py     # 从 stock_analyst.py 迁入
        │       ├── sector.py    # 从 sector_analyst.py 迁入
        │       └── event.py     # 从 event_analyst.py 迁入
        │
        ├── tools/               # LangChain @tool，按数据域分组
        │   ├── base.py          # 通用 @tool 基类 + safe_tool_call 装饰器（Phase 4 新增）
        │   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
        │   ├── sector_tools.py  # get_leader_stocks
        │   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
        │   ├── market_tools.py  # get_global_markets（yfinance）, tavily_finance_search
        │   ├── monitor_tools.py # 占位（Phase 5 实现）
        │   └── tenx_tools.py    # 占位（Phase 5 实现）
        │
        ├── prompts/             # 提示词集中管理（Phase 4 分层对应 agents 目录）
        │   ├── supervisor/routing.py  # 从 routing.py 迁入
        │   ├── general/system.py      # GENERAL_PROMPT
        │   └── workers/               # morning.py / stock.py / sector.py / event.py
        │
        ├── services/            # 全局资源封装
        │   ├── llm.py           # 模型工厂（从 agents/base.py 迁移，Phase 4）
        │   └── data_client.py   # httpx AsyncClient → Node.js /internal/* API
        │
        └── api/
            ├── routes.py        # REST 接口（/chat/message + /chat/stream SSE + /briefing/morning + /skills）
            ├── deps.py          # 依赖注入（verify_internal_token / build_initial_state）（Phase 4 新增）
            └── ws.py            # WebSocket 流式接口
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
    intent: Optional[str]          # morning | stock | wind_leader | event_chain | alert | tenx | forecast | review | general
    symbol: Optional[str]          # 提取的股票代码
    tag_code: Optional[str]        # 提取的板块代码
    # 分析报告累积（多步分析时复用，参考TradingAgents）
    analysis_reports: dict[str, str]
    # 最终响应
    final_response: Optional[str]
```

---

## 6. Graph 设计

### 6.1 主流程 Graph

```
START
  │
  ▼
supervisor（quick_think）
  │ 写入 state.intent
  │
  ├─── intent="morning"       ──▶  morning_agent
  ├─── intent="stock"         ──▶  stock_analyst
  ├─── intent="wind_leader"   ──▶  wind_leader_agent
  ├─── intent="event_chain"   ──▶  event_chain_agent
  ├─── intent="alert"         ──▶  alert_agent
  ├─── intent="hot_burst"     ──▶  hot_burst_agent
  ├─── intent="tenx"          ──▶  tenx_agent
  ├─── intent="forecast"      ──▶  forecast_agent
  ├─── intent="review"        ──▶  review_agent
  └─── intent="general"       ──▶  general_agent
                │
                ▼
          broadcast_agent（播报生成）
                │
                ▼
               END
```

### 6.2 多专家 Agent 协作体系（参考涨乐AI）

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

### 6.3 条件边（graph/edges.py）

```python
def route_by_intent(state: AgentState) -> str:
    intent = state.get("intent", "general")
    valid = {
        "morning", "stock", "wind_leader", "event_chain",
        "alert", "hot_burst", "tenx", "forecast", "review", "general"
    }
    return intent if intent in valid else "general"
```

---

## 7. Agent 详细设计

### 7.1 Supervisor Agent

- **模型**：quick_think（gpt-4o-mini）
- **职责**：意图分类，写入 `state.intent` 和 `state.symbol` / `state.tag_code`
- **不调用任何工具**，纯LLM分类
- **路由意图列表**：morning / stock / wind_leader / event_chain / alert / tenx / forecast / review / general

### 7.2 Morning Agent（早点听/晨报）★

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

### 7.3 Stock Analyst Agent（个股分析）

- **模型**：deep_think
- **工具集**：`get_quote` + `get_capital_flow` + `get_profit_forecast` + `search_cls_news`
- **能力**：个股行情 + 资金流向 + 机构预测 + 相关新闻综合分析
- **产品对应**：App 个股详情页 + AI 对话个股问答

### 7.4 Wind Leader Agent（长线风口/风口龙头）

- **模型**：deep_think
- **工具集**：`get_leader_stocks` + `get_wind_leaders` + `get_capital_flow`
- **能力**：板块龙头筛选 + 风口持续性研判 + 板块资金动向分析
- **产品对应**：App 长线风口页（泡泡图 + 龙头卡列表）
- **网页端参考**：WindLeaderPanel（风口龙头面板，含AI研判持续性评分）
- **涨乐AI参考**：涨停猎手/热点捕手的板块龙头筛选逻辑

### 7.5 Event Chain Agent（事件传导链）

- **模型**：deep_think
- **工具集**：`search_cls_news` + `get_news_fulltext` + `get_quote` + `tavily_finance_search`
- **能力**：事件识别 → 重要性评分(5级) → 传导链分析（事件→行业→个股）→ 历史回溯
- **产品对应**：App 事件传导页（因果链可视化 + RelationGraph 组件）
- **网页端参考**：AiGraph（AI产业链图谱，提供行业-个股关联脉络数据）
- **涨乐AI参考**：事件捕手/事件传导的5级评分和因果链可视化

**事件传导链分析框架：**
1. 事件识别：LLM从新闻中提取核心事件
2. 重要性评分：5-4-3-2-1分制（5=国家级政策，1=日常经营动态）
3. 传导路径推演：事件 → 受影响行业 → 受影响个股（结合AiGraph产业链图谱数据）
4. 定量+定性分析：估算事件对个股净利润的弹性影响
5. 历史回溯：标注类似历史事件在股价对应时间点的涨跌

### 7.6 Alert Agent（异动提醒/持仓监控）

- **模型**：quick_think（异动识别不需要深度推理，快速分类即可）
- **工具集**：`get_stock_monitor` + `get_quote` + `get_capital_flow` + `search_cls_news`
- **能力**：自选股/持仓股异动检测 → 异动类型分类 → AI简短解读 → 应对建议
- **产品对应**：App 特别提醒页（异动时间线 + 便签式提醒）
- **网页端参考**：StockMonitorView（个股异动监控）
- **涨乐AI参考**：特别提醒的便签式设计（发生了什么→为什么→怎么办）

**异动检测类型：**
- 价格异动：5分钟涨跌幅超阈值 / 量比异常
- 资金异动：主力资金大额进出
- 基本面异动：业绩预告 / 重大公告 / 监管问询
- 事件异动：关联重大事件触发

### 7.7 Tenx Agent（十倍股/趋势股评分）

- **模型**：deep_think
- **工具集**：`get_tenx_score` + `get_quote` + `get_capital_flow` + `get_profit_forecast`
- **能力**：多维度评分（6维度18指标）→ 趋势起始点判断 → 潜力排序
- **产品对应**：App 十倍股评分页 + 趋势股发现
- **网页端参考**：TenxScoreView（6维度18指标评分体系）
- **团队关联**：陈菲负责的"趋势股起始点判断逻辑"研究

**评分维度（参考网页端已有体系）：**
1. 成长空间：行业增速、新业务占比、市场空间
2. 竞争格局：行业集中度、市占率变化
3. 经营绩效：ROE、营收增速、资产负债率
4. 资金面：主力资金持续流入、机构持仓变化
5. 技术面：趋势突破信号、量价配合
6. 催化剂：政策/事件/业绩等催化因素

### 7.8 Hot Burst Agent（机构调研热门股）

- **模型**：deep_think（共振模型判断需要深度推理）
- **工具集**：`get_hot_burst` + `get_hot_burst_history` + `get_quote` + `tavily_finance_search`
- **能力**：四信号源共振检测 → AI解读共振原因 → 持续性判断
- **产品对应**：App 机构调研热门股页
- **网页端参考**：HotBurstView（聚合格隆汇/财联社快讯 + 同花顺热点掘金 + 研报验证）
- **后端参考**：HotBurstService.ts（现有三步检测和四信号源共振模型）
- **团队关联**：吴涵晶负责的 Agent + App 前端接入

**四信号源共振模型（现有后端已实现）**：
1. 格隆汇/财联社快讯（媒体关注度）
2. 同花顺热点掘金（平台热度）
3. 飞书研报验证（机构背书）
4. 股价异动（市场反应）

**Agent 增强点**：
- 在现有共振检测基础上增加 AI 解读能力
- 解读共振原因（为什么这只股票被多方关注）
- 判断持续性（是短期炒作还是长期风口）

### 7.9 Forecast Agent（业绩预测）— 后续

- **模型**：quick_think（数据整理为主，不需要深度推理）
- **工具集**：`get_profit_forecast` + `get_quote` + `search_cls_news`
- **能力**：业绩预告/预测数据聚合 → AI提炼核心要点 → 机构一致预期整理
- **产品对应**：App 业绩预测页
- **网页端参考**：PerformanceForecastView（业绩预测列表+搜索+排序）
- **团队关联**：后续开发，不安排本周

**输出格式：**
- 股票名称、代码、预测EPS、评级、机构数量
- 业绩预告AI提炼（把冗长公告变成几句话）
- 机构一致预期 vs 实际业绩偏差分析

### 7.10 Review Agent（交易复盘）— P2

- **模型**：deep_think
- **工具集**：`get_quote` + 历史交易数据接口（待设计）
- **能力**：交易行为分析 → 高光/待改进标记 → 优化建议
- **产品对应**：App 交易复盘页
- **涨乐AI参考**：交易复盘的操作合理性评估和优化空间识别
- **说明**：P2 优先级，依赖历史交易数据接口，Phase 5+ 实现

### 7.11 General Agent（兜底对话）

- **模型**：quick_think
- **工具集**：`get_quote`（基础行情）
- **能力**：关键词触发基础查询，兜底未匹配意图

### 7.12 Broadcast Agent（播报生成）★ 核心特色

- **模型**：deep_think（需要深度推理生成对话式播报）
- **职责**：汇聚各 Agent 分析结果 → 生成双人对话格式播报内容
- **能力**：多 Agent 结果整合 → 对话式解读 → 播报脚本生成
- **产品对应**：App 早点听播报 / 异动提醒播报 / 各类播报场景
- **涨乐AI参考**：双人播报（AI分析师 + AI主持人对话式解读）
- **团队关联**：组长负责设计 + App 播报效果实现

**播报格式设计**（双人对话式）：
```json
{
  "title": "晨报播报 - 2026-07-06",
  "segments": [
    {
      "role": "host",
      "content": "各位投资者早上好，今天有哪些重要信息需要关注？"
    },
    {
      "role": "analyst",
      "content": "昨晚美股三大指数集体收涨，S&P500涨1.2%，纳指涨1.5%..."
    },
    {
      "role": "host",
      "content": "这对A股开盘有什么影响？"
    },
    {
      "role": "analyst",
      "content": "预计A股开盘偏暖，但需关注..."
    }
  ],
  "audio_urls": [],  // TTS 生成的音频链接（可选）
  "duration": "3min"
}
```

**播报场景覆盖**：
- 晨报播报（morning_agent 结果）
- 异动提醒播报（alert_agent 结果）
- 风口龙头播报（wind_leader_agent 结果）
- 事件传导播报（event_chain_agent 结果）

**前端联动**：
- 前端拿到播报 JSON 后，调用 TTS API 生成语音
- 分角色朗读（AI分析师声音 + AI主持人声音）
- 支持暂停/快进/倍速播放

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
GET /internal/wind-leaders           → WindLeaderService.getWindLeaders()          ★ 新增
GET /internal/monitor/:symbol        → StockMonitorService.getMonitorData()        ★ 新增
GET /internal/tenx/score/:symbol     → TenxScoreService.getScore()                 ★ 新增
GET /internal/tenx/top               → TenxScoreService.getTopStocks()             ★ 新增
GET /internal/graph/concepts         → IndustryKGService.getConcepts()              ★ 新增
GET /internal/graph/:concept         → IndustryKGService.getGraphByConcept()        ★ 新增
GET /internal/institution-research   → HotBurstService.getHotBurst()               ★ 新增
GET /internal/institution-research/history → HotBurstService.getHotBurstHistory() ★ 新增
```

这些接口仅供Python服务内部调用，不对外暴露，建议加内网鉴权（IP白名单或固定Header）。

---

## 9. API 接口设计（Python侧）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/chat/message` | 对话消息（非流式） |
| POST | `/chat/stream` | 对话消息（SSE流式） |
| GET | `/briefing/morning` | 晨报（SSE流式，支持Redis缓存）|
| GET | `/alert/list` | 异动提醒列表 ★ 新增 |
| GET | `/tenx/score/:symbol` | 十倍股评分 ★ 新增 |
| GET | `/tenx/top` | 十倍股排行 ★ 新增 |
| GET | `/forecast/list` | 业绩预测列表 ★ 新增 |
| GET | `/event/chain/:id` | 事件传导链详情 ★ 新增 |
| GET | `/skills` | 已注册工具列表 |
| GET | `/health` | 健康检查 |

Node.js侧将 `/api/agent/*` 的请求反代到Python服务对应路径。

---

## 10. 分阶段实施计划

| Phase | 内容 | 核心产出 | 验收标准 | 状态 |
|-------|------|----------|----------|------|
| **1** | 项目骨架 | pyproject.toml / config / AgentState / FastAPI `/health` | `uvicorn`启动，`/health` 返回200 | ✅ 完成 |
| **2** | Node.js内部API + Python Tools层 | 6个`/internal/*`接口 + 5个`@tool` | 每个tool有pytest，独立可调用 | ✅ 完成 |
| **3** | Morning Agent | `agents/workers/morning.py`（Phase 4 迁移） + Redis缓存 + SSE接口 | `/briefing/morning` SSE流式返回4步分析 | ✅ 完成 |
| **4** | 核心对话Agent层 | 物理分层重构（agents/services/graph/prompts/utils/schemas/memory/errors）+ supervisor + stock/sector/event/general/morning agent + graph builder + checkpointer 持久化 + 异常降级 + /chat/stream SSE + 三层测试 | 完整消息流程：输入→路由→工具调用→回复；多轮对话可恢复；146 测试全绿 | ✅ 完成 |
| **5** | Node.js接入 + 新增Internal API | Express反代 + 新增6个`/internal/*`接口 + 对应Tools | 端到端测试通过 | ⏳ 待开始 |
| **6** | 产品功能Agent | alert + tenx + forecast agent | 各Agent独立可调用，有pytest | ⏳ 待开始 |
| **7** | 交易复盘 + 标准文档 | review_agent + `AGENT_STANDARDS.md` | 全部Agent可用，文档覆盖所有扩展场景 | ⏳ 待开始 |

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

- [x] Python 服务端口：8000（已在 .env.example 确认）
- [x] Redis：与 Node.js 共用，`redis://localhost:6379/1`
- [x] Tavily API Key：7 Key 轮换池，已在 config.py + .env.example 实现
- [x] 服务器 Python 环境：3.11

---

*本文档在实施过程中持续更新。架构变更需同步修改本文档。*
