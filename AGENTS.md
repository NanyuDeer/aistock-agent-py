# AGENTS.md - aistock-agent-py

> 本文件是 AI Agent 的入口地图，开发者与 AI 对话前必读

## 项目概述
AiStock Agent 推理服务，基于 Python FastAPI + LangGraph，负责多 Agent 编排和深度推理。
Node.js 后端（aistock-app-api）负责数据层和 HTTP 接入，本服务专注推理。

## 技术栈
- 框架: FastAPI + uvicorn
- Agent 编排: LangGraph + LangChain
- LLM: langchain-openai（支持 DeepSeek/OpenAI）
- 缓存: Redis（会话持久化 + 晨报缓存）
- 境外数据: yfinance（美股/亚太/大宗/汇率）
- 全网搜索: Tavily
- 配置: pydantic-settings

## 核心架构

### Graph 拓扑
```
START → supervisor(quick_think)
  ├── intent="morning"  → morning_agent(deep_think)
  ├── intent="stock"    → stock_analyst(deep_think)
  ├── intent="sector"   → sector_analyst(deep_think)
  ├── intent="event"    → event_analyst(deep_think)
  └── intent="general"  → general_agent(quick_think)
→ END
```

### 数据流
- Python 通过 `services/data_client.py`（httpx）回调 Node.js `/internal/*` 获取 A 股数据
- 境外市场数据（yfinance）和全网搜索（Tavily）在 Python 侧直接调用
- **禁止在 Python 重复实现 A 股数据获取逻辑**

### 双模型策略
- `quick_think`（gpt-4o-mini）：意图分类、兜底对话
- `deep_think`（gpt-4o）：晨报分析、个股/板块/事件深度分析

## 目录结构
```
src/aistock_agent/
├── main.py              # FastAPI 入口（/health, CORS, 路由注册）
├── config.py            # pydantic-settings 读取环境变量
├── state/
│   └── schema.py        # AgentState TypedDict
├── graph/
│   ├── edges.py         # 条件边函数
│   └── builder.py       # StateGraph 构建 + compile()
├── agents/
│   ├── base.py          # 双模型工厂
│   ├── supervisor.py    # 意图分类（quick_think）
│   ├── morning_agent.py # 晨报（ReAct + Redis 缓存）
│   ├── stock_analyst.py # 个股分析
│   ├── sector_analyst.py# 板块分析
│   ├── event_analyst.py # 事件传导链
│   └── general_agent.py # 兜底
├── tools/
│   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
│   ├── sector_tools.py  # get_leader_stocks
│   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
│   └── market_tools.py  # get_global_markets, tavily_finance_search
├── prompts/
│   ├── morning.py       # 晨报4步框架
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

## 常用命令
```bash
uvicorn aistock_agent.main:app --reload  # 开发模式
pytest tests/ -v                         # 运行测试
ruff check src/                          # 代码检查
mypy src/                                # 类型检查
```

## 关键约束
- 禁止在 Python 重复实现 A 股数据获取逻辑
- LLM 调用失败时返回降级文本，不重试
- yfinance 仅用于境外市场数据（美股/亚太/大宗/汇率）
- 晨报 Agent 必须通过 Redis 缓存，同一天不重复调用 deep_think
