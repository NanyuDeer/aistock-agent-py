# AI Stock Agent Service

> LangGraph 多 Agent 智能体服务（Python），负责意图识别、Agent 编排和深度推理。
> Node.js 后端（aistock-app-api）作为数据层和 HTTP 接入层，Python 服务专注推理。

## 快速开始

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -e ".[dev]"

# 复制环境变量
cp .env.example .env
# 编辑 .env 填入实际配置

# 启动开发服务
uvicorn aistock_agent.main:app --reload --port 8000

# 运行测试
pytest tests/ -v
```

## 架构

### 服务边界

```
┌─────────────────────────────────────────┐
│  Node.js Express API（数据层）           │
│  · A股数据Service（Tencent/Sina/THS）   │
│  · /api/agent/* → 反代到 Python 服务     │
│  · /internal/* → Python 专用数据接口     │
└──────────────┬──────────────────────────┘
               │ HTTP + SSE
┌──────────────▼──────────────────────────┐
│  Python FastAPI Agent 服务（推理层）     │
│  · LangGraph 图编排                      │
│  · 通过 /internal/* 回调 Node.js 拿数据  │
│  · yfinance：境外指数/大宗/汇率           │
│  · Tavily：全网财经新闻搜索              │
└─────────────────────────────────────────┘
```

**原则：Python 服务不拥有数据，只拥有推理。A 股实时数据留 Node.js。**

### 目录结构

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
│   ├── base.py          # 双模型工厂（quick_think / deep_think）
│   ├── supervisor.py    # 意图分类节点
│   ├── morning_agent.py # 晨报 Agent（ReAct + Redis 缓存）
│   ├── stock_analyst.py # 个股分析 Agent
│   ├── sector_analyst.py# 板块分析 Agent
│   ├── event_analyst.py # 事件传导 Agent
│   └── general_agent.py # 兜底 Agent
├── tools/
│   ├── stock_tools.py   # get_quote, get_capital_flow, get_profit_forecast
│   ├── sector_tools.py  # get_leader_stocks
│   ├── news_tools.py    # search_cls_news, get_news_fulltext, get_cls_news
│   └── market_tools.py  # get_global_markets(yfinance), tavily_finance_search
├── prompts/
│   ├── morning.py       # 晨报4步框架提示词
│   ├── routing.py       # 路由分类提示词
│   └── system.py        # 通用系统提示词
├── services/
│   └── data_client.py   # httpx → Node.js /internal/* API
└── api/
    ├── routes.py        # REST 接口
    └── ws.py            # WebSocket 流式接口
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/chat/message` | 对话消息（非流式） |
| GET | `/api/agent/briefing/morning` | 晨报（SSE 流式，支持 Redis 缓存） |
| GET | `/api/agent/skills` | 已注册工具列表 |
| GET | `/health` | 健康检查 |

Node.js 侧将 `/api/agent/*` 的请求反代到 Python 服务对应路径。

## Graph 拓扑

```
START → supervisor(quick_think)
  ├── intent="morning"  → morning_agent(deep_think)
  ├── intent="stock"    → stock_analyst(deep_think)
  ├── intent="sector"   → sector_analyst(deep_think)
  ├── intent="event"    → event_analyst(deep_think)
  └── intent="general"  → general_agent(quick_think)
→ END
```

## 双模型策略

| 用途 | 模型 | 原因 |
|------|------|------|
| 意图分类/路由 | quick_think（gpt-4o-mini） | 低延迟，成本低 |
| 深度分析/晨报/事件 | deep_think（gpt-4o） | 推理质量优先 |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NODE_API_BASE_URL` | Node.js 后端地址 | `http://localhost:3000` |
| `OPENAI_API_KEY` | OpenAI API 密钥 | - |
| `OPENAI_BASE_URL` | OpenAI API 基础 URL | `https://api.openai.com/v1` |
| `QUICK_THINK_MODEL` | 快速模型名称 | `gpt-4o-mini` |
| `DEEP_THINK_MODEL` | 深度模型名称 | `gpt-4o` |
| `REDIS_URL` | Redis 连接地址 | `redis://localhost:6379/1` |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | - |
| `INTERNAL_API_TOKEN` | 内网鉴权 Token | `change-me-in-production` |

## 测试

```bash
# 运行全部测试（mock 模式，不需要实际服务）
pytest tests/ -v

# 运行单个模块测试
pytest tests/test_stock_tools.py -v
```

## 部署

```bash
# Docker 构建
docker build -t aistock-agent .

# 运行
docker run -p 8000:8000 --env-file .env aistock-agent
```

## 相关项目

- [aistock-app-api](../aistock-app-api) — Node.js 后端（数据层 + HTTP 接入）
- [aistock-app-frontend](../aistock-app-frontend) — App 前端
