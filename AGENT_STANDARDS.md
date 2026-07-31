# AGENT_STANDARDS.md — Agent 开发标准

> 本文件是 `aistock-agent-py` 的 Agent 开发规范，覆盖 8 个核心开发规范 + 4 个补充规范，
> 供团队后续扩展 Agent / Tool 时参考。所有规范均提取自**当前代码库的实际模式**，
> 非前瞻性建议。新增 Agent / Tool 前请先阅读本文件对应章节。
>
> 配套文档：`README.md`（项目概述、架构、API、环境变量）、`AGENTS.md`（AI 开发入口地图、
> 目录结构、异常降级规范）、`docs/refactor-plan.md`（重构设计与分阶段计划）。

---

## 目录

- [规范 1：State-first 原则](#规范-1state-first-原则)
- [规范 2：新增 Tool 流程](#规范-2新增-tool-流程)
- [规范 3：新增 Agent 流程](#规范-3新增-agent-流程)
- [规范 4：提示词管理](#规范-4提示词管理)
- [规范 5：错误处理规范](#规范-5错误处理规范)
- [规范 6：双模型使用规则](#规范-6双模型使用规则)
- [规范 7：缓存规范](#规范-7缓存规范)
- [规范 8：测试覆盖要求](#规范-8测试覆盖要求)
- [补充规范 9：可观测性标准](#补充规范-9可观测性标准)
- [补充规范 10：API 接口标准](#补充规范-10api-接口标准)
- [补充规范 11：配置标准](#补充规范-11配置标准)
- [补充规范 12：代码风格](#补充规范-12代码风格)
- [补充规范 13：空数据预检](#补充规范-13空数据预检)
- [补充规范 14：报告双层输出](#补充规范-14报告双层输出)
- [附录 A：目录结构速查](#附录-a目录结构速查)
- [附录 B：常用命令速查](#附录-b常用命令速查)

---

## 规范 1：State-first 原则

**核心规则**：所有数据通过 `AgentState` 流转，禁止节点间隐式传递。新增状态字段必须修改
`state/schema.py`。

### 为什么

LangGraph 的状态机模型依赖 `AgentState` 在节点间传递数据。若节点通过闭包变量、模块级
全局变量、或函数参数隐式传递数据，会破坏图的可恢复性（checkpointer 无法持久化隐式数据）
和可观测性（trace 看不到隐式数据流转）。

### 现状参考

`src/aistock_agent/state/schema.py`：

```python
from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import NotRequired, TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage | dict[str, str]], add_messages]
    session_id: str
    user_id: str | None
    favorites: list[str]
    # 路由信息（supervisor 写入）
    intent: str | None
    symbol: str | None
    tag_code: str | None
    # 分析报告累积
    analysis_reports: dict[str, str]
    # 预加载字段（Python入口写入，Agent读取；NotRequired 允许 scheduler state 省略）
    wind_leaders_data: NotRequired[dict[str, object] | None]
    institution_research_data: NotRequired[dict[str, object] | None]
    # 最终响应
    final_response: str | None
```

- `messages` 用 `Annotated[..., add_messages]` reducer：节点返回 `{"messages": [...]}` 时
  **追加**而非覆盖，保留完整对话历史。
- `intent` / `symbol` / `tag_code` 由 supervisor 节点写入，下游 worker 节点读取——这是
  节点间数据流转的唯一合法方式。
- `final_response` 由 worker 节点写入，API 层读取后返回前端。

### 节点返回值契约

每个节点的 `run(state)` 返回一个 **dict patch**（不是完整 state），LangGraph 会按字段
reducer 合并到全局 state：

```python
# supervisor 写入路由字段
return {"intent": "stock", "symbol": "600519", "tag_code": None}

# worker 写入最终响应
return {"final_response": "贵州茅台最新价1688元..."}

# 降级路径也必须返回 dict patch（不抛异常）
return {"final_response": "个股分析暂时不可用，请稍后重试"}
```

### 新增状态字段流程

1. 在 `state/schema.py` 的 `AgentState` 中新增字段，附 `Attributes` 文档注释。
2. 若新字段并非所有调用方都需要传入（如 scheduler 构造的 state），用 `NotRequired[T]` 标记为可选（需 `from typing_extensions import NotRequired`）。
3. 若新字段需要"追加而非覆盖"语义，用 `Annotated[T, <reducer>]`。
4. 在 `api/deps.py` 的 `build_initial_state` 中补默认值（若字段由入口构造）。
5. 检查所有 `morning_briefing` 等手写 state 字典（`api/routes.py:117`）是否需同步补字段。
6. 更新 `tests/` 中所有构造 state 的测试 fixture。

### 禁止

- 在节点函数内用 `global` 或模块级变量传递状态。
- 把 `state` 当成不可变参数透传——节点只返回需要更新的字段。
- 在 `AgentState` 之外定义"影子状态"（如 `session_store` 单例缓存未进入 state 的数据）。

---

## 规范 2：新增 Tool 流程

**核心规则**：Tool 是 `@tool` + `@safe_tool_call` 装饰的 async 函数，通过 `NodeApiClient`
拿数据，返回 `str`，异常自动降级。命名用 `get_xxx` / `search_xxx`。

### 标准模板

参考 `src/aistock_agent/tools/stock_tools.py`：

```python
"""个股行情工具 — 通过 Node.js /internal/* API 获取 A 股数据"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_quote(symbol: str) -> str:
    """查询 A 股个股实时行情

    Args:
        symbol: 6位股票代码，如 600519（贵州茅台）
    """
    data = await node_api.get(f"/internal/quote/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的行情数据"
    return _format_quote(data)


def _format_quote(data: dict[str, object]) -> str:
    """格式化行情数据（腾讯数据源，中文 key）"""
    name = data.get("股票简称", "未知")
    price = data.get("最新价", "-")
    change_pct = data.get("涨跌幅", "-")
    return f"【{name}】最新价: {price}  涨跌幅: {change_pct}%"
```

### 装饰器顺序（关键）

**`@tool` 在上，`@safe_tool_call` 在下**。顺序反了会让 langchain 拿到装饰器 wrapper 的
签名而非业务签名，导致生成的 tool schema 错误（参数名丢失、docstring 不生效）。

`@safe_tool_call` 用 `functools.wraps` 保留 `__name__` / `__doc__` / signature，确保
`@tool` 能据 docstring 生成正确的参数 schema。

### 强制要求清单

| 要求 | 说明 | 正面示例 |
|------|------|----------|
| 命名规范 | 数据查询用 `get_xxx`，搜索用 `search_xxx`，列表用 `get_xxx_top` / `get_xxx_history` | `get_quote` / `search_cls_news` / `get_tenx_top_stocks` |
| async | 所有 tool 必须 async（`safe_tool_call` 仅支持 async） | `async def get_quote(...)` |
| 类型注解 | 参数必须有类型注解，禁止 `any`（用 `str` / `int` / `str \| None`） | `async def get_alert_history(symbol: str \| None = None, days: int = 7) -> str` |
| docstring + `Args:` | docstring 必须含 `Args:` 段，langchain 据此生成参数描述 | 见上例 |
| 返回 `str` | tool 必须返回 `str`（LLM 的 observation 是文本） | `return f"【{name}】..."` |
| 走 `NodeApiClient` | A 股数据走 `node_api.get` / `node_api.get_list`，禁止 Python 侧直连数据源 | `data = await node_api.get(f"/internal/quote/{symbol}")` |
| 空数据降级 | 数据为空返回友好提示文本（非 None、非异常） | `if not data: return f"未找到股票 {symbol} 的行情数据"` |
| 格式化抽离 | 复杂格式化抽到私有 `_format_xxx` 函数，保持 tool 函数简洁 | `return _format_quote(data)` |

### 列表型端点用 `get_list`

Node.js 部分接口的 `data` 字段是数组（如 `/internal/monitor/:symbol`、`/internal/graph/concepts`）。
`node_api.get` 只返回 dict（`isinstance(data, dict)` 判否返回 None），列表端点必须用
`node_api.get_list`：

```python
# tools/monitor_tools.py
data = await node_api.get_list(f"/internal/monitor/{symbol}")
if not data:
    return f"未找到股票 {symbol} 的监控数据"
return _format_events(data, title=f"【{symbol}】监控事件")
```

### 例外：Python 侧直连的数据源

只有两类工具可在 Python 侧直连第三方，**不走 Node.js**：

1. **yfinance**（境外市场数据，美股/亚太/大宗/汇率）——见 `tools/market_tools.py` 的 `get_global_markets`。
2. **Tavily**（全网财经新闻搜索）——见 `tools/search_tools.py` 的 `tavily_finance_search`（底层调用 `services/tavily.py` 的 `TavilyService`）。

这两类是 Node.js 无对应实现的例外。**A 股数据禁止在 Python 侧重复实现获取逻辑**（项目硬约束）。

### 注册到 Tool Registry（自动注册）

新增 tool 后，在**定义该 tool 的文件底部**调用 `register()` 自注册，**不需要编辑 `registry.py`**：

```python
# tools/stock_tools.py — 文件底部
from aistock_agent.tools.registry import register

register("stock", get_quote)           # 主 category
register("stock", get_capital_flow)
register("stock", get_profit_forecast)
register("event", get_quote)           # 跨 category 共享
register("sector", get_capital_flow)   # 跨 category 共享
```

`register()` 会在模块加载时自动执行（`tools/__init__.py` 导入所有 tool 模块触发）。
同一工具可注册到多个 category，重复注册同一 category 会自动去重。

Agent 通过 `get_tools("category")` 获取工具集，不再手动 import + 拼接：

```python
# agents/workers/stock.py
from aistock_agent.tools.registry import get_tools
tools = get_tools("stock")
agent = create_react_agent(llm, tools)
```

**注意**：集成测试使用**集合断言**（`set(t.name for t in tools) == EXPECTED_TOOL_NAMES`），
不依赖工具顺序，多人新增工具不会互相影响。

### 暴露给 `/skills` 接口

`register()` 默认 `expose=True`，工具会自动出现在 `GET /api/agent/skills`：

```python
# tools/review_tools.py — 跨 category 注册示例
# 复盘工具集是跨 category 组合：复用其他 category 的工具 + 新增专属工具
from aistock_agent.tools.market_tools import get_global_markets
from aistock_agent.tools.news_tools import get_cls_news
from aistock_agent.tools.search_tools import tavily_finance_search

register("review", tavily_finance_search)    # 跨 category 共享（原属 search）
register("review", get_global_markets)       # 跨 category 共享（原属 market）
register("review", get_cls_news)             # 跨 category 共享（原属 news）
register("review", get_market_summary)       # review 专属（expose=True，前端可见）
register("review", get_sector_performance)   # review 专属（expose=True，前端可见）
# 如需隐藏某工具不出现在 /skills：register("review", tool, expose=False)
```

**跨 category 注册要点**：同一工具可注册到多个 category（如 `tavily_finance_search` 同时属于 `"search"` 和 `"review"`），`register()` 自动去重。复盘 agent 通过 `get_tools("review")` 一次性获取全部 5 个工具，无需手动 import + 拼接。

`api/routes.py` 的 `list_skills` 不再手动维护 `all_tools`，直接调用 `get_exposed_skills()`。

### 绑定到 Agent

Tool 创建后，在对应 worker agent 的 `run()` 中绑定到 `create_react_agent`：

```python
# agents/workers/stock.py
from aistock_agent.tools.registry import get_tools

tools = get_tools("stock")
agent = create_react_agent(llm, tools)
```

### 测试要求

每个 tool 必须有 mock 测试（详见[规范 8](#规范-8测试覆盖要求)），放 `tests/unit/test_<module>_tools.py`。

---

## 规范 3：新增 Agent 流程

**核心规则**：业务 Agent 放 `agents/workers/<name>.py`，实现 `async def run(state) -> dict`，
顶层 try-catch 返回降级文本。在 `graph/builder.py` 注册节点，在
`graph/routers/intent_router.py` 加路由条件，在 `prompts/workers/<name>.py` 放提示词。

### 物理分层约束（Phase 4 硬约束）

`agents/` 目录物理分层，禁止混放：

| 子目录 | 职责 | 模型 |
|--------|------|------|
| `agents/supervisor/` | 路由决策节点（意图分类） | quick_think |
| `agents/general/` | 兜底通用节点 | quick_think |
| `agents/workers/` | 深度业务专业智能体 | deep_think（多数）/ quick_think（少数） |

### 标准模板

参考 `src/aistock_agent/agents/workers/stock.py`（最简模板）：

```python
"""Stock Analyst Agent — 个股综合分析

工具集：get_quote, get_capital_flow, get_profit_forecast, search_cls_news
"""

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.stock import STOCK_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.news_tools import search_cls_news
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """个股分析：行情 + 资金流向 + 机构预测 + 相关新闻"""
    symbol = state.get("symbol")
    if not symbol:
        return {"final_response": "请提供股票代码，例如：分析一下 600519"}

    try:
        llm = get_deep_think()
        tools = [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=STOCK_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))
        return {"final_response": final_response}
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="stock_analyst",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "个股分析暂时不可用，请稍后重试"}
```

### run() 函数契约

| 要素 | 规则 |
|------|------|
| 签名 | `async def run(state: AgentState) -> dict[str, object]` |
| 入口校验 | 缺少必要输入（如 `symbol`）时提前返回提示文本，不调用 LLM |
| 模型获取 | 通过 `services.llm.get_deep_think()` / `get_quick_think()` 获取，**禁止**在 `agents/` 内持有模型工厂逻辑 |
| 工具绑定 | `agent = create_react_agent(llm, tools)`，tools 是已注册的 `@tool` 函数列表 |
| 消息构造 | `SystemMessage(content=<PROMPT>)` + `*state.get("messages", [])[-5:]`（最近 5 条上下文） |
| 响应提取 | 统一用 `utils.message.extract_final_ai_response`，禁止手写"取最后一条 AI 消息"逻辑 |
| 返回值 | `{"final_response": <str>}`，降级路径同样返回此结构 |
| 异常处理 | 顶层 `try/except Exception`，structlog 记录 `agent_run_failed` + agent 名，返回降级文本 |

### 新增 Agent 的 6 步流程

以新增 `forecast` Agent 为例：

**Step 1：创建提示词** —— `src/aistock_agent/prompts/workers/forecast.py`

```python
"""业绩预测分析师提示词 — 由 SYSTEM_PROMPT 派生"""
from aistock_agent.prompts.general.system import SYSTEM_PROMPT

FORECAST_ANALYST_PROMPT = SYSTEM_PROMPT + """

你是业绩预测分析师。根据用户提供的股票代码，整理：
- 机构盈利预测（EPS、评级、机构数量）
- 业绩预告 AI 提炼
- 机构一致预期 vs 实际业绩偏差
"""
```

**Step 2：创建 Agent 文件** —— `src/aistock_agent/agents/workers/forecast.py`

按上面的标准模板，import `FORECAST_ANALYST_PROMPT`，绑定 `get_profit_forecast` 等工具，
实现 `run()`。降级文本：`"业绩预测暂时不可用，请稍后重试"`。

**Step 3：注册节点** —— `src/aistock_agent/graph/builder.py`

```python
from aistock_agent.agents.workers import forecast as forecast_agent

graph.add_node("forecast_agent", forecast_agent.run)
# 加入 agent_nodes 列表（用于 add_edge(node, END)）
agent_nodes = [
    "morning_agent", "stock_analyst", "sector_analyst",
    "event_analyst", "forecast_agent", "general_agent",
]
```

**Step 4：加路由条件** —— `src/aistock_agent/graph/routers/intent_router.py`

```python
VALID_INTENTS = {"morning", "stock", "sector", "event", "forecast", "general"}

node_map = {
    ...
    "forecast": "forecast_agent",
    "general": "general_agent",
}
```

同时同步 `src/aistock_agent/constants.py` 的 `INTENT_SET`（注：当前 `INTENT_SET` 与
`VALID_INTENTS` 是平行定义，新增 intent 需两处同步——已知 minor finding，后续应改为 import 复用）。

**Step 5：更新 supervisor 提示词** —— `src/aistock_agent/prompts/supervisor/routing.py`

在 `ROUTING_PROMPT` 的意图列表中加入 `forecast`，让 LLM 知道可以输出该 intent。

**Step 6：编写测试** —— `tests/integration/test_forecast_agent.py`

详见[规范 8](#规范-8测试覆盖要求)。

### 模型选择不在 `services/llm.py` 绑定

`services/llm.py` 只提供 `get_quick_think()` / `get_deep_think()` 两个工厂函数，**不**为
每个 agent 绑定专属模型。模型选择由 agent 自身的 `run()` 决定（调哪个工厂函数）。
新增模型参数（temperature / max_tokens）走 config，详见[补充规范 11](#补充规范-11配置标准)。

### 例外模式 A：定时触发型 ReAct Agent（复盘 review）

复盘 agent（`agents/workers/review.py`）与标准 worker 有三点关键差异：

| 维度 | 标准 worker（如 stock） | 复盘 worker（review） |
|------|------------------------|----------------------|
| 触发方式 | supervisor 路由（用户请求） | scheduler 定时触发（交易日 15:30） |
| 图注册 | `graph/builder.py` 注册节点 | **不注册到图**，scheduler 直接调 `review.run(state)` |
| 路由条件 | `intent_router.py` 加 intent | **无 intent**，不经过 supervisor |

**其余完全遵循标准 worker 模式**：ReAct 架构（`create_react_agent`）、`get_tools("review")` 获取工具集、顶层 try-catch 降级、`extract_final_ai_response` 提取响应。

```python
# agents/workers/review.py — 核心逻辑
async def run(state: AgentState) -> dict[str, object]:
    try:
        cached = await get_cached_review()      # Redis 缓存（briefing:review:YYYY-MM-DD）
        if cached:
            return {"final_response": cached}

        llm = get_deep_think()
        tools = get_tools("review")              # 跨 category 工具集
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke({"messages": [SystemMessage(content=system_prompt)]})

        final_response = extract_final_ai_response(result.get("messages", []))
        if final_response:
            await set_cached_review(final_response)  # 写 Redis 缓存
            _archive_review(final_response)           # 归档到文件
        return {"final_response": final_response}
    except Exception as e:
        logger.error("agent_run_failed", agent="review", error=str(e), exc_info=True)
        return {"final_response": "复盘生成暂时不可用，请稍后重试"}
```

**5 步归因 prompt**（`prompts/workers/review.py`）：步骤 1 罗列核心变量（事实层）→ 步骤 2 匹配行情特征（数据层）→ 步骤 3 剔除噪音（排除层）→ 步骤 4 输出核心结论（归因层）→ 步骤 5 **强制输出标准化行情事实附录**（供迭代 agent 解析，表格格式）。

**工具集注册**（`tools/review_tools.py`）：复盘工具集是跨 category 组合——复用 `tavily_finance_search`（search category）、`get_global_markets`（market category）、`get_cls_news`（news category）+ 新增 `get_market_summary`、`get_sector_performance`（review category），全部注册到 `"review"` category：

```python
# tools/review_tools.py — 文件底部
register("review", tavily_finance_search)    # 跨 category 共享
register("review", get_global_markets)       # 跨 category 共享
register("review", get_cls_news)             # 跨 category 共享
register("review", get_market_summary)       # review 专属
register("review", get_sector_performance)   # review 专属
```

**缓存 + 归档**：Redis key `briefing:review:YYYY-MM-DD`（TTL=2h，与晨报同域），文件归档到 `docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md`。

### 例外模式 B：定时触发型 Pipeline + LLM Agent（迭代 iterate）

迭代 agent（`agents/workers/iterate.py`）是最特殊的 worker——**不是 ReAct agent**，是纯流水线 + LLM：

```
读快照文件 → 读 rolling_stats → 代码判断阈值 → 全正常则返回 / 触发则 LLM 生成分析 → 归档
```

| 维度 | 标准 worker | 迭代 worker（iterate） |
|------|------------|----------------------|
| 架构 | `create_react_agent`（ReAct） | **无 ReAct**，直接 `llm.invoke()` |
| 工具 | `get_tools()` 绑定工具集 | **无工具**，纯 LLM 单次调用 |
| 触发 | supervisor / scheduler | scheduler 定时触发（交易日 15:40） |
| 输入 | `state["messages"]` | **读文件**（snapshot JSON + rolling_stats JSON） |
| 输出 | `{"final_response": <str>}` | `{"final_response": <JSON string>}` |
| 权限 | 读写 state | **只读 + 建议**，禁止修改任何文件 |

**硬编码阈值**（`check_thresholds` 函数，LLM 不可改）：

| 维度 | 触发条件 | 回看窗口 |
|------|----------|----------|
| 维度一（关注点重叠） | `hit_rate < 0.5` 或 `new_coverage_rate > 0.4` | MA5 |
| 维度二（方向-强度） | `abs(mean_deviation) > 3` 或 MA10 均值偏差 `> 1.5` | 当日 + MA10 |
| 维度三（归因一致性） | `attribution_match_rate < 0.3` | 当日 + MA5 |
| 维度四（情绪基调） | MA20 `abs(sentiment_bias) > 0.15` | MA20 |

阈值判断由代码完成（`check_thresholds` 返回触发的维度列表），仅当有维度触发时才调用 LLM 生成偏差分析报告。全部正常时返回 `{"status": "normal"}`（仍包含四维评分卡），不消耗 LLM token。

**确定性评分卡**（`build_scorecard` 函数，LLM 不可改）：无论是否触发阈值，输出始终包含 `scorecard` 字段，覆盖全部四个维度，每个维度记录实际指标值（`metrics`）、阈值描述（`thresholds`）、是否触发（`triggered`）。

**JSON 输出与输出清洗**：迭代 agent 返回结构化 JSON，归档到 `docs/agent-outputs/iterate/YYYY-MM-DD.json`。LLM 输出经 `_sanitize_llm_output` 清洗后归档，清洗规则：

| 字段 | 来源 | 清洗规则 |
|------|------|----------|
| `triggered_dimensions` | `check_thresholds()` 确定性结果 | 始终覆盖 LLM 返回值，LLM 不可改 |
| `scorecard` | `build_scorecard()` 确定性结果 | 始终由代码生成，LLM 不可改 |
| `analysis` | LLM 生成 | 只保留已触发维度；未触发维度的分析降级到 `observations` |
| `optimization_suggestions` | LLM 生成 | 每条需标注 `dimension` 字段；只保留基于已触发维度的建议，未触发的降级到 `observations` |
| `observations` | 代码降级 | 低优先级区域，存放从 `analysis`/`suggestions` 降级的未触发维度内容，标注来源和降级原因 |

LLM 输出非 JSON 时包装为 `raw_text` 降级。

**只读约束**：prompt 明确约束"你只能读取数据和生成建议，不能修改任何文件"。优化建议产出后由人工审核，不自动回写 prompt / 代码 / 数据文件。

### 例外模式 C：快照生成器（非 Agent，service 层代码 + LLM 混合）

快照生成器（`services/snapshot_builder.py`）**不是 agent**，是 service 层的流水线中间件。代码控制流程，LLM 只做语义判断。

**代码层 / LLM 层职责边界**（核心设计原则）：

| 层 | 职责 | 可确定性 |
|----|------|----------|
| 代码层 | 文件读写、JSON 组装、MA 计算、manifest 维护、板块字典第一级匹配、异常降级 | 确定性，不可被 LLM 覆盖 |
| LLM 层 | 板块语义匹配（第二级）、方向-强度打分、归因相似度、情绪分析 | 语义判断，有降级默认值 |

**两级板块匹配机制**：

1. **第一级（代码层，`match_sectors_code_level`）**：使用 `data/sector_aliases.json`（35 标准板块 → 别名列表）做别名映射，将晨报和复盘的板块列表匹配。采用标准名集合求交（一个别名可能对应多个标准名，如"贵金属"同时归属黄金与白银），避免 last-write-wins 字典在别名碰撞时丢失映射。
2. **第二级（LLM 层，`llm_evaluate_dimensions`）**：代码层未匹配的板块交由 LLM 做语义等价判断，发现的新别名自动追加到 `sector_aliases.json`（`_append_new_aliases`），实现字典自学习。

**MA 计算**（`calculate_ma`）：基于 manifest 历史记录计算 MA5/MA10/MA20 滑动平均，指标包括 `hit_rate` / `direction_accuracy` / `mean_deviation` / `attribution_match_rate` / `sentiment_bias`。

**Manifest 维护**：每次生成快照后，向 `docs/agent-outputs/manifest.json` 追加一条记录（日期 + 5 个核心指标），并更新 `docs/agent-outputs/rolling_stats.json`（MA5/MA10/MA20）。

**LLM 输出校验**（`_validate_llm_dimension`）：LLM 返回的 JSON 经 `json.loads` 后做轻量 schema 校验——每个维度必须是 dict，数值字段必须是 int/float（排除 bool），校验失败降级到默认零值。LLM 整体失败时返回全零值快照 + `error` 标记，不中断流水线。

**降级路径**：晨报或复盘文件缺失时返回零值快照（标注 `error: "missing_reports"`），不调用 LLM。

---

## 规范 4：提示词管理

**核心规则**：提示词统一放 `prompts/` 对应子目录，日期等动态内容用 `{{PLACEHOLDER}}`
占位运行时替换，禁止代码内硬编码长提示词。

### 目录结构

`prompts/` 分层对应 `agents/` 目录：

```
prompts/
├── supervisor/
│   └── routing.py        # ROUTING_PROMPT（意图分类）
├── general/
│   └── system.py         # SYSTEM_PROMPT（基础常量）+ GENERAL_PROMPT
└── workers/
    ├── morning.py        # MORNING_PROMPT（4步框架）
    ├── stock.py          # STOCK_ANALYST_PROMPT
    ├── sector.py         # SECTOR_ANALYST_PROMPT
    └── event.py          # EVENT_ANALYST_PROMPT
```

### 基础常量 + 派生模式

`prompts/general/system.py` 定义 `SYSTEM_PROMPT` 基础常量（核心原则），各 worker 提示词
import 后拼接专属职责：

```python
# prompts/general/system.py
SYSTEM_PROMPT = """你是 AiStock 智能投资助手，专注 A 股市场分析。

核心原则：
1. 所有数据通过工具获取，不编造数据
2. 数据获取失败时标注"数据暂不可用"，不猜测
3. 分析客观中立，不预测具体涨跌幅
4. 策略建议标注"仅供参考，不构成投资建议"
"""

# prompts/workers/stock.py
from aistock_agent.prompts.general.system import SYSTEM_PROMPT

STOCK_ANALYST_PROMPT = SYSTEM_PROMPT + """

你是个股分析师。根据用户提供的股票代码，综合分析：
- 实时行情（价格、涨跌、成交量）
- 资金流向（主力净流入/流出）
...
"""
```

新增 worker 提示词应复用 `SYSTEM_PROMPT`，避免重复"核心原则"。

### 动态内容用占位符

日期、用户名等运行时变化的内容用 `{{PLACEHOLDER}}` 占位，运行时 `.replace()` 替换。
参考 `agents/workers/morning.py`：

```python
from aistock_agent.prompts.workers.morning import MORNING_PROMPT

today = datetime.now().strftime("%Y年%m月%d日")
system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
```

**禁止**在 agent 代码中用 f-string 拼接长提示词——长文本必须在 `prompts/` 模块中定义为常量。

### 版本变更加注释

提示词是 LLM 行为的核心，变更加注释说明原因和日期：

```python
# prompts/workers/morning.py
MORNING_PROMPT = """你是 AiStock 晨报分析师，日期：{{DATE}}。
# 2026-07-06: 第4步增加"不预测具体涨跌"约束（响应合规要求）
...
"""
```

### 禁止

- 在 agent `run()` 中硬编码超过 3 行的提示词文本。
- 用 `input()` / 配置文件动态注入提示词（提示词必须可静态审计）。
- 把提示词放在 `agents/` 目录（必须放 `prompts/` 对应子目录）。

---

## 规范 5：错误处理规范

**核心规则**：两层降级体系——Tool 层 `@safe_tool_call` 捕获异常返回降级文本，Agent 层
`run()` 顶层 try-catch 返回降级文本。禁止异常中断图执行。降级文本标注"暂不可用"。

### 两层降级体系

```
用户请求
   │
   ▼
graph.ainvoke
   │
   ▼
worker agent run()
   │  ┌─────────────────────────────────────────┐
   │  │ Agent 层降级（顶层 try/except Exception） │
   │  │ 捕获 LLM/Graph 框架异常                   │
   │  │ → structlog error + 返回降级文本          │
   │  └─────────────────────────────────────────┘
   │
   ▼
create_react_agent → tool 执行
   │  ┌─────────────────────────────────────────┐
   │  │ Tool 层降级（@safe_tool_call）            │
   │  │ 捕获工具异常（node_api 失败/yfinance 异常）│
   │  │ → structlog error + 返回 DEGRADED_MESSAGE │
   │  └─────────────────────────────────────────┘
   │
   ▼
LLM 收到降级文本作为 observation，按 prompt 要求在最终回复标注"数据暂不可用"
```

### Tool 层：`@safe_tool_call`（tools/base.py）

装饰器统一捕获工具异常，返回稳定的降级文本：

```python
# tools/base.py
DEGRADED_MESSAGE = "数据暂不可用，请稍后重试"

def safe_tool_call(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error("tool_call_failed", tool=func.__name__, error=str(e), exc_info=True)
            return DEGRADED_MESSAGE
    return wrapper
```

- **稳定不变**：`DEGRADED_MESSAGE` 是固定字符串，前端和测试可据此断言。
- **不抛异常**：graph 收到降级文本而非崩溃，继续执行。
- **falsy 透传**：正常返回值（含空串）原样透传，不被吞掉。

### Agent 层：`run()` 顶层 try-catch

每个 `run()` 必须有顶层 `try/except Exception`，捕获 LLM/Graph 框架异常（`get_deep_think()`
失败、`create_react_agent()` 失败、`ainvoke()` 失败），返回符合规范的降级文本：

```python
async def run(state: AgentState) -> dict[str, object]:
    try:
        llm = get_deep_think()
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke(...)
        final_response = extract_final_ai_response(result.get("messages", []))
        return {"final_response": final_response}
    except Exception as e:
        logger.error("agent_run_failed", agent="stock_analyst", error=str(e), exc_info=True)
        return {"final_response": "个股分析暂时不可用，请稍后重试"}
```

### 降级文本（每个 agent 不同，便于日志区分）

| Agent | 降级文本 |
|-------|---------|
| supervisor | `{"intent": "general"}`（路由降级到 general 兜底） |
| morning | `{"final_response": "晨报生成暂时不可用，请稍后重试"}` |
| stock | `{"final_response": "个股分析暂时不可用，请稍后重试"}` |
| sector | `{"final_response": "板块分析暂时不可用，请稍后重试"}` |
| event | `{"final_response": "事件分析暂时不可用，请稍后重试"}` |
| review | `{"final_response": "复盘生成暂时不可用，请稍后重试"}` |
| iterate | `{"final_response": '{"date":"...","status":"error","summary":"迭代分析失败: ..."}'}`（JSON 格式） |
| alert | `{"final_response": "异动提醒暂时不可用，请稍后重试"}` |
| general | `{"final_response": "抱歉，我暂时无法处理您的请求，请稍后重试"}` |

新增 worker agent 必须遵循同一格式：`"<功能名>暂时不可用，请稍后重试"`。

### 不做异常分类 catch（硬约束）

**只 catch `Exception` 一层**，不写 `except ToolExecutionError` / `except LLMTimeoutError`。

`errors/exceptions.py` 定义了 `ToolExecutionError` / `LLMTimeoutError` / `DataUnavailableError`
/ `RouteError` 异常类（含 `code` 属性），但**当前业务代码无显式抛出点**，是预留的 dead code。
未来有显式抛出场景再补分类 catch，当前分类 catch 是 dead code。

参考 `project_memory.md` 的"Phase 4 重构约束"：不做异常分类 catch。

### LLM 调用失败不重试

LLM 调用失败时返回降级文本，**不重试**（防烧钱，项目硬约束）。`@safe_tool_call` 和
agent 层 try-catch 都是单次捕获，无 retry 逻辑。

### 禁止

- 在 tool 函数内 `raise` 异常期望上层处理（必须由 `@safe_tool_call` 降级）。
- 在 `run()` 中不写 try-catch 让异常穿透到 graph（会中断图执行）。
- 降级文本中猜测数据（如"贵州茅台最新价1688元"——必须标注"暂不可用"）。

---

## 规范 6：双模型使用规则

**核心规则**：`quick_think`（gpt-4o-mini）用于低延迟任务，`deep_think`（gpt-4o）用于
深度推理。temperature / max_tokens 从 config 读取，禁止硬编码。

### 模型分配表

| Agent | 模型 | 原因 |
|-------|------|------|
| supervisor（意图分类） | quick_think | 低延迟，成本低 |
| general（兜底对话） | quick_think | 简单问答 |
| alert（异动提醒） | deep_think | 三步异动分析（什么→为什么→怎么办）需要跨工具综合推理 |
| forecast（业绩预测） | quick_think | 数据整理为主 |
| morning（晨报） | deep_think | 4步宏观策略分析 |
| stock（个股分析） | deep_think | 多维度综合分析 |
| sector（板块分析） | deep_think | 龙头筛选 + 资金研判 |
| event（事件传导链） | deep_think | 6步传导链 + pgvector 语义匹配 + 双层输出（display_report + podcast_brief） |
| review（复盘归因） | deep_think | 5步归因分析 + 标准化行情事实附录 |
| iterate（迭代分析） | deep_think | 偏差分析 + 优化建议（仅阈值触发时调用 LLM） |
| tenx（十倍股评分） | deep_think | 6维度18指标评分 |
| broadcast（播报生成） | deep_think | 对话式播报生成 |

### 判断原则

- **需要推理 / 多步分析 / 长文本生成** → `deep_think`
- **分类 / 路由 / 简短问答 / 数据整理** → `quick_think`

新增 agent 时按此原则选择，并在 `AGENTS.md` 的"产品功能 → Agent 映射"表中标注模型。

### 模型工厂在 `services/llm.py`

`get_quick_think()` / `get_deep_think()` 是唯一合法的模型获取入口，参数从 config 读取：

```python
# services/llm.py
def get_deep_think() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.deep_think_model,
        api_key=SecretStr(settings.openai_api_key),
        base_url=settings.openai_base_url,
        temperature=settings.deep_think_temperature,
        max_tokens=settings.deep_think_max_tokens,
        callbacks=_get_observability_callbacks(),  # 自动挂载可观测性回调
    )
```

**禁止**：

- 在 `agents/` 内直接 `from langchain_openai import ChatOpenAI` 构造模型。
- 在 agent 代码中硬编码 `temperature=0.3` / `max_tokens=4000`。
- 在 agent 代码中手动挂载 observability callback（由 `services/llm.py` 统一挂载）。

### 新增第三种模型

若未来需要 `reasoning_think`（如 o1 推理模型），流程：

1. `config.py` 加 `reasoning_think_model` / `reasoning_think_temperature` / `reasoning_think_max_tokens`。
2. `services/llm.py` 加 `get_reasoning_think()` 工厂函数。
3. `.env.example` 补字段示例。
4. `tests/unit/test_config.py` 加默认值断言。

---

## 规范 7：缓存规范

**核心规则**：缓存走 `services/cache.py`（基于 `RedisPool` 单例），key 格式
`<domain>:<sub>:<date>`，TTL 按业务幂等性设定。晨报 Redis TTL=2 小时。

### 当前缓存实现

`src/aistock_agent/services/cache.py` 提供晨报 + 复盘缓存：

```python
async def get_cached_briefing() -> str | None:
    client = await RedisPool.get_client()
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"briefing:morning:{today}"   # key 格式：<domain>:<sub>:<date>
    cached = await client.get(cache_key)
    ...

async def set_cached_briefing(content: str, ttl: int = 7200) -> None:  # TTL=7200s=2h
    client = await RedisPool.get_client()
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"briefing:morning:{today}"
    await client.setex(cache_key, ttl, content)

# 复盘缓存（结构完全对称，key 域名 briefing:review:）
async def get_cached_review() -> str | None:
    ...   # briefing:review:{YYYY-MM-DD}

async def set_cached_review(content: str, ttl: int = 7200) -> None:
    ...   # briefing:review:{YYYY-MM-DD}
```

### Key 格式规范

`<domain>:<sub>:<date>`，全小写，冒号分隔：

| 业务 | key 示例 | TTL |
|------|----------|-----|
| 晨报 | `briefing:morning:2026-07-08` | 7200s（2h） |
| 复盘 | `briefing:review:2026-07-08` | 7200s（2h） |
| （未来）异动播报 | `broadcast:alert:2026-07-08` | 按业务定 |
| （未来）风口播报 | `broadcast:wind:2026-07-08` | 按业务定 |

日期用 `YYYY-MM-DD`（`strftime("%Y-%m-%d")`），保证可排序、可读。

### 走 `RedisPool` 单例，禁止 `from_url`

**禁止**在业务代码中 `aioredis.from_url(settings.redis_url)` 创建连接。必须通过
`RedisPool.get_client()` 获取 lifespan 管理的连接池单例：

```python
# 正确
from aistock_agent.services.redis_pool import RedisPool
client = await RedisPool.get_client()

# 错误（Phase 5 已消除此模式）
import redis.asyncio as aioredis
client = aioredis.from_url(settings.redis_url)  # 每次请求创建连接，性能差
```

### 幂等性分析（哪些结果应缓存）

| 业务 | 是否缓存 | 理由 |
|------|----------|------|
| 晨报（morning） | ✅ 缓存 2h | 同一交易日的晨报内容幂等；deep_think 成本高（6000-8000 token/次） |
| 复盘（review） | ✅ 缓存 2h | 同一交易日的复盘内容幂等；deep_think 成本高 |
| 个股分析（stock） | ❌ 不缓存 | 行情实时变化，缓存会误导 |
| 事件传导（event） | ✅ 缓存 30min | 同一事件 30 分钟内结果幂等（MD5 key）；deep_think 成本高 |
| 板块分析（sector） | ❌ 不缓存 | 龙头/资金实时变化 |
| 异动提醒（alert） | ❌ 不缓存 | 异动定义就是"实时变化" |

**判断原则**：仅缓存"同一输入在同一时间段内结果幂等"且"生成成本高"的业务。晨报和复盘满足
（同一天分析内容稳定 + deep_think 贵），其他实时数据业务都不满足。

### 缓存异常不崩溃

`services/cache.py` 的 get/set 都用 `try/except` 包裹，Redis 异常时返回 None（get）或
静默失败（set），不抛异常中断业务：

```python
async def get_cached_briefing() -> str | None:
    try:
        ...
    except Exception:
        logger.debug("get_cached_briefing_failed", exc_info=True)
    return None  # 缓存异常视为未命中，走正常生成路径
```

### 缓存命中跳过 LLM

晨报 `run()` 和 `stream()` 都先查缓存，命中直接返回，不调用 LLM（零 token 消耗）。复盘
`run()` 同理，先查 `get_cached_review()`，命中跳过 `get_deep_think()`：

```python
cached = await get_cached_briefing()  # 晨报（或 get_cached_review() 复盘）
if cached:
    return {"final_response": cached}  # 跳过 get_deep_think()
```

测试用哨兵验证（`tests/e2e/test_full_flow.py:test_full_flow_redis_cache_hit`）：
mock Redis 返回缓存，`get_deep_think` 设为"被调用即 AssertionError"，验证缓存命中路径
完全跳过 LLM。

---

## 规范 8：测试覆盖要求

**核心规则**：三层测试分层——`tests/unit/`（工具函数单测）、`tests/integration/`（Agent +
Graph 集成）、`tests/e2e/`（HTTP 端到端）。测试不依赖真实网络 / LLM / Redis。

### 三层分层

| 层次 | 目录 | 范围 | mock 策略 | 示例文件 |
|------|------|------|-----------|----------|
| unit | `tests/unit/` | 单个工具函数 / utils 函数 / config | mock `node_api` / `yfinance` / `TavilyClient` | `test_stock_tools.py` / `test_tools_base.py` |
| integration | `tests/integration/` | Agent `run()` 逻辑 + Graph 路由 | mock `create_react_agent` / `get_deep_think` / agent.run | `test_stock_agent.py` / `test_graph.py` |
| e2e | `tests/e2e/` | HTTP 端到端全链路 | mock LLM（`FakeToolCallingLLM`）+ `node_api` + `RedisPool` | `test_full_flow.py` / `test_chat_message.py` |

**禁止**往 `tests/` 根目录堆测试文件（Phase 4 硬约束）。

### 测试不依赖真实外部服务

- **不调真实 LLM API**（零 token 消耗）：用 `FakeToolCallingLLM`（`tests/e2e/test_full_flow.py`）
  或 `unittest.mock.MagicMock` mock `get_quick_think` / `get_deep_think`。
- **不依赖真实 Redis**：mock `services.cache.RedisPool`（`tests/conftest.py:mock_redis`）。
- **不依赖真实 Node.js**：mock `node_api.get` / `node_api.get_list`。
- **不依赖真实 yfinance / Tavily**：mock `tools.market_tools.yf` / `aistock_agent.services.tavily.TavilyClient`。

### Tool 单元测试模式

参考 `tests/unit/test_stock_tools.py`：patch 消费方模块的 `node_api`（不是源模块）：

```python
@pytest.mark.asyncio
async def test_get_quote_success():
    mock_data = {"股票简称": "贵州茅台", "最新价": 1688.00, "涨跌幅": 0.75}
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_quote.ainvoke({"symbol": "600519"})
        assert "贵州茅台" in result
        assert "1688" in result
        mock_api.get.assert_called_once_with("/internal/quote/600519")
```

**关键**：`from ... import node_api` 在 import 时把单例引用复制到各 tool 模块，故必须 patch
**消费方模块**（`aistock_agent.tools.stock_tools.node_api`）而非源模块
（`aistock_agent.services.data_client.node_api`）。

每个 tool 至少覆盖：正常返回 + 空数据（None）+ 异常降级（`@safe_tool_call` 行为）。

### Agent 集成测试模式

参考 `tests/integration/test_stock_agent.py`：mock `create_react_agent` 和 `get_deep_think`，
验证工具集绑定 + SystemMessage 注入 + final_response 提取 + 入口校验：

```python
_CREATE_REACT_AGENT = "aistock_agent.agents.workers.stock.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.stock.get_deep_think"

@pytest.mark.asyncio
async def test_stock_agent_tools_bound_correctly():
    mock_agent = _make_mock_agent([AIMessage(content="分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]})
    tools_arg = mock_create.call_args[0][1]
    assert tools_arg == [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]
```

每个 agent 至少覆盖：工具集绑定 + 提示词注入 + 响应提取 + 入口校验 + 异常降级（5 个 case）。

### E2E 端到端测试模式

参考 `tests/e2e/test_full_flow.py`：用 `httpx.ASGITransport(app=app)` 在进程内跑 FastAPI，
mock LLM（`FakeToolCallingLLM`）让真实 `create_react_agent` + 真实 `@tool` 函数执行：

```python
class FakeToolCallingLLM(BaseChatModel):
    """按序列返回预置 AIMessage 的假 LLM，兼容 create_react_agent。"""
    responses: list[AIMessage] = []
    _idx: int = PrivateAttr(default=0)
    def bind_tools(self, tools, **kwargs): return self  # 假模型不需要真绑定
    ...

async with httpx.AsyncClient(
    transport=httpx.ASGITransport(app=app), base_url="http://test",
) as client:
    resp = await client.post(_CHAT_URL, json={...}, headers=_VALID_HEADERS)
```

E2E 覆盖：5 类意图全链路（stock/sector/event/general/morning SSE）+ 工具失败降级 +
Redis 缓存命中。

### 共享 fixture

`tests/conftest.py` 提供：

- `mock_node_api`：patch `NodeApiClient`，返回预设数据。
- `mock_yfinance`：patch `tools.market_tools.yf`。
- `mock_tavily`：patch `aistock_agent.services.tavily.TavilyClient`（TavilyService 模块级 import，patch 服务层）。
- `mock_redis`：patch `services.cache.RedisPool`，注入 mock client。

### 异步测试配置

`pyproject.toml` 配置 `asyncio_mode = "strict"`，异步测试必须显式标 `@pytest.mark.asyncio`。

---

## 补充规范 9：可观测性标准

**核心规则**：可观测性通过 callback / middleware 解耦，**零侵入**业务逻辑。业务代码禁止
直接调用 structlog（除 `logger = structlog.get_logger()` 模块级声明和 agent 降级日志）。

### 三层可观测性

| 层次 | 实现 | 触发方式 | 数据 |
|------|------|----------|------|
| 日志 | `observability/logging.py`（structlog JSON） | `setup_logging()` 在 `main.py` 启动时调用 | timestamp/level/event/request_id |
| 回调 | `observability/callback.py`（LangChain callback） | `services/llm.py` 挂载到 ChatOpenAI + `builder.py` 挂载到图 | token 用量 + agent 步骤 |
| 指标 | `observability/metrics.py`（MetricsCollector） | 回调 handler 写入，`get_metrics()` 读取 | llm_calls/errors/tool_calls/tokens |

### 日志：`structlog.get_logger()` 与 `get_logger(name)` 两种入口

`observability/logging.py` 提供两种获取 logger 的方式：

1. **`get_logger(name: str) -> structlog.BoundLogger`** —— 类型安全的封装，内部 `cast` 为
   `BoundLogger`（绕过 structlog 返回 `Any` 的类型问题）。**新代码推荐使用**。
2. **`structlog.get_logger()`** —— 直接调用 structlog，返回 `Any`（mypy strict 下需 cast）。

**当前代码库现状**：大多数业务模块（`agents/workers/*`、`agents/general/node.py`、
`agents/supervisor/node.py`、`api/routes.py`、`services/*`、`tools/base.py`、
`memory/checkpointer.py`）直接 `import structlog` + `logger = structlog.get_logger()`。
仅 `main.py`、`api/middleware.py`、`observability/callback.py` 使用封装的 `get_logger()`。

**新增代码推荐**：用 `from aistock_agent.observability.logging import get_logger` +
`logger = get_logger(__name__)`，避免 `Any` 类型问题（与"禁止 any"约束一致）。

```python
# 推荐（新代码）
from aistock_agent.observability.logging import get_logger
logger = get_logger(__name__)
logger.error("agent_run_failed", agent="stock", error=str(e), exc_info=True)

# 现状（历史代码，可工作但不推荐新代码沿用）
import structlog
logger = structlog.get_logger()  # 返回 Any，mypy strict 下类型不安全
```

无论用哪种入口，日志调用方式一致：`logger.error("event_name", key=value, exc_info=True)`，
structlog 会自动合并 contextvar（含 `request_id`）。

### 回调：自动挂载，业务无感

`TokenUsageCallback` / `AgentTraceCallback` 由 `services/llm.py` 在构造 `ChatOpenAI` 时
通过 `callbacks=` 挂载，agent 节点和工具函数完全不感知：

```python
# services/llm.py
def get_deep_think() -> ChatOpenAI:
    return ChatOpenAI(
        ...
        callbacks=_get_observability_callbacks(),  # 自动挂载
    )
```

图级回调由 `graph/builder.py` 的 `compile_graph()` 通过 `with_config(callbacks=...)` 自动挂载，
调用方无需显式传入。

**禁止**在 agent `run()` 或 tool 函数内手动创建 callback handler。

### request_id 自动注入

`api/middleware.py:request_id_middleware` 从 `X-Request-ID` header 取或生成 UUID，绑定到
structlog contextvar。下游所有日志通过 `merge_contextvars` 自动携带 `request_id`，
无需显式传递：

```python
# middleware 已绑定 contextvar，业务代码直接 log 即可
logger.error("agent_run_failed", ...)  # 自动带 request_id
```

### 指标：线程安全单例

`MetricsCollector` 是线程安全（`threading.Lock`）的模块级单例，回调 handler 写入，
`get_metrics()` 返回快照。业务代码不直接调用 `MetricsCollector`。

### LangSmith 追踪（可选）

`LANGSMITH_ENABLED=true` 时，`services/llm.py:_setup_langsmith_tracing` 设置 LangChain
环境变量，自动启用追踪。默认关闭，仅调试用。

---

## 补充规范 10：API 接口标准

**核心规则**：REST 接口在 `api/routes.py`，鉴权用 `Depends(verify_internal_token)`，
SSE 流式用 `EventSourceResponse`，错误返回 JSON 而非纯文本。

### 鉴权

所有 `/api/agent/*` 接口必须 `Depends(verify_internal_token)`（校验 `X-Internal-Token` header）：

```python
@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> ChatResponse:
    ...
```

例外：`/briefing/morning` 是公开接口（无鉴权），`/health` 和 `/health/ready` 不鉴权。

### 健康检查双端点

| 端点 | 用途 | 行为 |
|------|------|------|
| `GET /health` | liveness（K8s livenessProbe） | 始终返回 200，不检查依赖 |
| `GET /health/ready` | readiness（K8s readinessProbe） | 检查 Redis + Node.js + 可选 LLM，失败返回 503 + `status=degraded` |

liveness 不检查依赖：依赖抖动不应导致 K8s 重启健康进程。readiness 检查依赖：依赖不可用时
不接流量。LLM 检查默认关闭（`HEALTH_CHECK_LLM=false`），避免探针消耗 token。

### SSE 流式接口

SSE 用 `sse_starlette.EventSourceResponse` + async generator：

```python
@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, _: None = Depends(verify_internal_token)):
    async def generator() -> AsyncGenerator[dict[str, str], None]:
        async for event in graph.astream_events(initial_state, version="v2", config=...):
            sse_event = map_langgraph_event_to_sse(event)
            if sse_event is None:
                continue
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}
        yield {"data": json.dumps({"type": SSEEventType.DONE}, ensure_ascii=False)}
    return EventSourceResponse(generator())
```

SSE 事件类型用 `constants.SSEEventType` 常量，禁止 magic string。

### checkpointer 必须传 thread_id

`compile_graph()` 默认挂 checkpointer（Phase 5 硬约束）。`graph.ainvoke` / `astream` /
`astream_events` 必须传 `config={"configurable": {"thread_id": ...}}`，不传会抛
`ValueError: Checkpointer requires ... configurable keys`。

### 错误响应

- 业务错误：返回 200 + 降级文本（不是 500，详见[规范 5](#规范-5错误处理规范)）。
- 鉴权失败：403 + `{"detail": "Forbidden"}`。
- 未处理异常：`request_id_middleware` 捕获返回 500 + `{"detail": "Internal Server Error"}`
  + `X-Request-ID` header（确保可追溯）。
- 全局异常处理器（`global_exception_handler`）：防御性兜底，确保异常穿透中间件时仍返回 JSON。

### 中间件注册顺序

`api/middleware.py:setup_middleware` 注册顺序（Starlette prepend，最后注册 = 最外层）：

1. `CORSMiddleware`（最内层）
2. `access_log_middleware`
3. `request_id_middleware`（最外层）

request_id 在最外层确保所有请求（含 CORS 预检）都注入 request_id。

---

## 补充规范 11：配置标准

**核心规则**：所有配置走 `pydantic-settings` 的 `Settings` 类，从 `.env` 或环境变量读取。
禁止在代码中硬编码可配置值。

### 新增配置字段流程

1. 在 `src/aistock_agent/config.py` 的 `Settings` 类加字段，附默认值和注释：

```python
class Settings(BaseSettings):
    # 新模型参数
    reasoning_think_model: str = "o1"
    reasoning_think_temperature: float = 0.5
    reasoning_think_max_tokens: int = 8000
```

2. 在 `.env.example` 补字段示例。
3. 在 `README.md` 的"环境变量"表补说明。
4. 在 `tests/unit/test_config.py` 加默认值 + env 覆盖断言。

### 配置分类

| 类别 | 字段示例 | 说明 |
|------|----------|------|
| LLM | `openai_api_key` / `quick_think_model` / `deep_think_temperature` | 模型与参数 |
| Redis | `redis_url` / `redis_max_connections` | 连接池 |
| HTTP | `node_api_base_url` / `http_timeout_seconds` / `internal_api_token` | Node.js API 调用 |
| 服务 | `host` / `port` / `log_level` | 进程配置 |
| CORS | `cors_origins` | 跨域（支持逗号分隔或 JSON 数组） |
| 健康检查 | `health_check_llm` | readiness 探针 |
| LangSmith | `langsmith_enabled` / `langsmith_api_key` / `langsmith_project` | 追踪 |
| checkpointer | `checkpointer_backend` / `sqlite_path` | LangGraph 持久化后端 |
| Tavily | `tavily_api_key` / `tavily_api_keys` | 全网搜索（支持多 key 轮换） |

### 特殊字段处理

- `cors_origins`：用 `Annotated[list[str], NoDecode]` + `@field_validator(mode="before")`
  支持逗号分隔和 JSON 数组两种格式。
- `tavily_api_keys`：多 key 池，`get_tavily_key()` 随机选取，支持多成员共享额度。
- `log_level`：大小写不敏感（config.py 的 `@field_validator(mode="before")` 强制转大写，防御 `LOG_LEVEL=info` 等小写环境变量覆盖）；`_parse_level` 无效值回退 INFO。

### 模块级单例

`config.py` 末尾 `settings = Settings()` 创建模块级单例，业务代码 `from ...config import settings`
直接使用，禁止重复 `Settings()` 实例化。

---

## 补充规范 12：代码风格

**核心规则**：Python ≥ 3.11，禁止 `any`（用 `object` 或具体类型），ruff + mypy strict
必须 clean，类型注解全覆盖。

### Python 版本

`pyproject.toml` 声明 `requires-python = ">=3.11"`，`ruff.target-version = "py311"`，
`mypy.python_version = "3.11"`。可使用 3.11+ 特性（`str | None` 联合类型、`list[str]` 泛型、
`match` 语句等）。

### 禁止 `any`

项目硬约束（aistock-workflow rules）：**禁止 `any`，用 `unknown`**。Python 对应：

- 不确定类型用 `object`（不是 `Any`）。
- 容器类型用 `dict[str, object]` / `list[object]`（不是 `dict[str, Any]`）。
- 函数返回不确定类型用 `object` 或具体类型。

```python
# 正确
def _format_quote(data: dict[str, object]) -> str: ...
async def get(self, path: str) -> dict[str, object] | None: ...

# 错误
def _format_quote(data: dict[str, Any]) -> str: ...
```

**注**：`src/` 严格无 `Any`（mypy strict + ruff）。`tests/` 历史代码有少量 `Any`（ruff 不
检查 tests/），新测试代码应用 `object`（Phase 4 终审已 sweep 部分测试代码）。

### 类型注解全覆盖

- 函数参数和返回值必须有类型注解。
- 模块级变量和类属性应有类型注解（mypy strict 要求）。
- 回调 handler 的 `**kwargs: object` 用 `object`（不是 `Any`）。

### ruff 规则

`pyproject.toml` 启用 `["E", "F", "I", "N", "W", "UP"]`：

- `E`/`W`：pycodestyle 错误和警告
- `F`：pyflakes（未使用 import、未定义变量等）
- `I`：import 排序
- `N`：命名规范
- `UP`：pyupgrade（自动升级到新语法）

`line-length = 100`。

### mypy strict

`pyproject.toml` 配置 `[tool.mypy] strict = true`，要求：

- 所有函数有类型注解。
- 不允许隐式 `Any`。
- 不允许未类型化的定义。

**已知例外**：`langchain_openai.ChatOpenAI` 的 `max_tokens` 参数是 Pydantic Field，
mypy 无 plugin 无法识别，用 `# type: ignore[call-arg]` 抑制（见 `services/llm.py`）。

### import 风格

- 标准库 → 第三方 → 本项目，三段式（ruff `I` 自动排序）。
- `from __future__ import annotations` 仅在需要时用（Python 3.11+ 多数场景不需要）。

### docstring 风格

- 模块 docstring：说明文件职责。
- 函数 docstring：说明行为 + `Args:` + `Returns:` + `Raises:`（如有）。
- 类 docstring：说明职责 + 属性。

参考 `tools/base.py` / `observability/callback.py` 的 docstring 风格。

---

## 补充规范 13：空数据预检

**核心规则**：依赖 Node.js 后端预计算数据的 Agent，在 `run()` 调用 `create_react_agent`
之前应预检数据可用性。空数据时调用刷新接口重试（最多3次），3次后仍空返回降级文本，
不调用 LLM（节省 token）。

### 适用范围

| 场景 | 是否预检 | 原因 |
|------|----------|------|
| Agent 依赖 Node.js 预计算数据 + 有刷新接口 | ✅ 预检 | 数据可能尚未生成，刷新可触发计算 |
| Agent 依赖 Node.js 数据但无刷新接口 | ⏭️ 可选 | 仅重试查询，效果有限 |
| Agent 仅依赖外部 API（Tavily/yfinance） | ❌ 豁免 | 无法通过 Node.js 预检 |
| Agent 空数据有业务意义（如 hot_burst） | ❌ 豁免 | 空数据是正常结果 |

### 预检函数

`services/data_guard.py` 提供通用的 `ensure_data_available` 函数和 `DataCheck` 数据类：

```python
from aistock_agent.services.data_guard import DataCheck, ensure_data_available

# 定义检查项
checks = [
    DataCheck(
        check_path="/internal/wind-leaders",        # 查询路径
        refresh_path="/api/cn/wind-leaders/refresh", # 刷新路径（可选）
        empty_checker=_is_data_empty,                # 空数据判定函数
        name="wind_leaders",                         # 日志名称
    )
]

# 预检（最多重试3次，每次间隔2秒）
if not await ensure_data_available(checks):
    return {"final_response": "XXX暂时不可用：后端数据源为空，请稍后重试"}
```

### 标准模板

参考 `agents/workers/wind_leader.py`：

```python
def _is_wind_leaders_empty(data: dict[str, object] | None) -> bool:
    """检查风口龙头数据是否为空"""
    if not data:
        return True
    sectors = data.get("hot_sectors")
    return not isinstance(sectors, list) or len(sectors) == 0


async def run(state: AgentState) -> dict[str, object]:
    try:
        # 预检：仅 scheduler 触发时执行（用户实时请求不等待）
        if state.get("trigger_source") == "scheduler":
            checks = [
                DataCheck(
                    check_path="/internal/wind-leaders",
                    refresh_path="/api/cn/wind-leaders/refresh",
                    empty_checker=_is_wind_leaders_empty,
                    name="wind_leaders",
                )
            ]
            if not await ensure_data_available(checks):
                return {"final_response": "长线风口分析暂时不可用：后端数据源为空，请稍后重试"}

        # 数据就绪，走 ReAct
        llm = get_deep_think()
        ...
```

### 设计要点

1. **仅 scheduler 触发时预检**：用户实时请求不等待（避免6秒延迟），工具本身有空数据降级处理。
2. **empty_checker 自定义**：每个 Agent 的"空数据"定义不同，需自定义检查函数。
3. **refresh_path 可选**：无刷新接口时设为 None，仅重试查询。
4. **重试间隔2秒**：给后端足够时间生成数据。
5. **预检失败不调用 LLM**：直接返回降级文本，节省 token。

### 新增 Agent 预检流程

1. 确认 Agent 是否依赖 Node.js 预计算数据（有 `/internal/*` 查询路径）。
2. 确认是否有对应的刷新接口（`/api/cn/*/refresh`）。
3. 编写 `_is_xxx_empty` 空数据判定函数。
4. 在 `run()` 开头添加预检代码（仅 scheduler 触发时）。
5. 更新本规范适用范围表。

### 禁止

- 对依赖外部 API 的 Agent 强行预检（Tavily/yfinance 无法通过 Node.js 预检）。
- 对空数据有业务意义的 Agent 预检（如 hot_burst）。
- 在用户实时请求时预检（避免等待延迟）。

---

## 补充规范 14：报告双层输出

**核心规则**：所有 Agent 持久化到数据库的 `content` 字段采用双层结构（schema_version 2.0），
一次 LLM 调用同时生成 `display_report`（前端展示）和 `podcast_brief`（播报摘要），
节省播报模型 token 消耗。

### 为什么

**两个核心原因：**

1. **前端展示需要**：前端页面需要"概要 + 完整报告内容"两层数据。`display_report.summary` 用于列表页/卡片快速浏览，`display_report.details` 用于详情页完整展示。单层 text 无法支撑结构化展示。
2. **省 token**：双人播报语音生成费用较高，不能把完整长报告喂给播报模型。传统单层 `{"text": "..."}` 结构导致 broadcast_agent 必须读取完整报告（500-1500字），token 消耗大。双层输出让 broadcast_agent 只读取 150-200 字的 `podcast_brief`，大幅降低成本。同理，ai_advisor_agent 读取 `display_report` 而非整份报告，也降低 token 消耗。

### content 字段结构

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

### 字段用途

| 字段 | 用途 | 字数要求 | 消费方 |
|------|------|---------|-------|
| `display_report` | 前端完整展示 | 无限制（500-1500字） | 前端页面直接读取 + `ai_advisor_agent` |
| `podcast_brief` | 播报原材料 | 150-200字/份 | `broadcast_agent` 汇总后生成双人对话 |

**关键约束**：`podcast_brief` 只保留主题、核心事实、判断结论、风险提示，不包含长篇分析。

### 解析工具

`utils/report_parser.py` 提供 4 个函数，自动兼容 schema_version 1.0（单层 `text`）和 2.0（双层）：

| 函数 | 用途 |
|------|------|
| `parse_report_content(content)` | 统一解析，返回 `{"display_report": str, "podcast_brief": str}` |
| `extract_podcast_brief(content)` | 提取播报摘要（优先 podcast_brief，降级取 display_report 前500字） |
| `extract_display_report(content)` | 提取展示文本（优先 display_report.details，降级取 text） |
| `parse_dual_layer_response(llm_response)` | 解析 LLM 的 JSON 双层输出，解析失败时降级为单层 |

### LLM 输出要求

Agent 提示词中须明确要求 LLM 在最终回复中输出 JSON 格式的双层内容。参考 `prompts/workers/wind_leader.py`：

```python
**最终输出格式**（完成工具调用后，最终回复必须严格按以下 JSON 格式返回，不要包含 ```json 标记）：

{
  "display_report": {
    "summary": "风口结论一句话（20字以内）",
    "details": "完整风口分析内容（500-1000字）",
    "stocks": ["龙头股代码1", "龙头股代码2"],
    "risks": ["风险提示1", "风险提示2"]
  },
  "podcast_brief": "150-200字的播报摘要"
}
```

`parse_dual_layer_response` 函数会解析 JSON，解析失败时降级为单层（`display_report.details = 原文本`），
不中断 Agent 流程。

### 持久化改造模板

参考 `agents/workers/wind_leader.py`：

```python
from aistock_agent.utils.report_parser import parse_dual_layer_response

# 持久化时使用 parse_dual_layer_response 解析 LLM 输出
dual_layer_content = parse_dual_layer_response(final_response)
await node_api.save_analysis_report(
    report_type="wind_leader",
    report_date=report_date,
    content=dual_layer_content,
)
```

### 消费方改造模板

参考 `agents/workers/broadcast.py` 和 `agents/workers/ai_advisor.py`：

```python
# broadcast_agent：优先读取 podcast_brief
from aistock_agent.utils.report_parser import extract_podcast_brief, extract_display_report

brief = extract_podcast_brief(content)
if brief:
    return brief
# 降级读取 display_report（兼容旧数据）
display = extract_display_report(content)
if display:
    return display[:500] if len(display) > 500 else display

# ai_advisor_agent：读取 display_report
from aistock_agent.utils.report_parser import extract_display_report

display_text = extract_display_report(data["content"])
if display_text:
    reports[report_type] = display_text
```

### 兼容性

- `report_parser.py` 自动兼容 1.0 单层 `{"text": "..."}` 和 2.0 双层结构
- 旧报告无需迁移，消费方代码自动降级处理
- `schema_version` 字段用于标识版本，1.0 无此字段（视为单层）

### 改造状态

| Agent | 状态 | 负责人 |
|-------|------|--------|
| wind_leader | ✅ 已改造 | 尹辰 |
| broadcast | ✅ 已改造（消费方） | 尹辰 |
| ai_advisor | ✅ 已改造（消费方） | 尹辰 |
| morning | ⏳ 待改造 | 王昌泽 |
| hot_burst | ⏳ 待改造 | 吴涵晶 |
| alert | ⏳ 待改造 | 李俊良 |

### 禁止

- 在 `podcast_brief` 中包含长篇分析（必须控制在 150-200 字）
- 直接把完整长报告喂给播报模型（必须使用 `podcast_brief`）
- 手动解析 content 字段（必须使用 `report_parser.py` 的函数）
- 在 Agent 代码中硬编码 JSON 解析逻辑（必须使用 `parse_dual_layer_response`）

---

## 附录 A：目录结构速查

```
aistock-agent-py/
├── pyproject.toml                    # 依赖 + ruff/mypy/pytest 配置
├── .env.example                      # 环境变量示例
├── Dockerfile
├── AGENT_STANDARDS.md                # 本文件（开发标准）
├── AGENTS.md                         # AI 开发入口地图
├── README.md                         # 项目概述、架构、API、环境变量
├── docs/
│   ├── refactor-plan.md              # 重构设计文档
│   ├── agent-outputs/morning/        # 晨报测试输出归档
│   └── superpowers/plans/            # 实施计划归档
├── scripts/
│   └── run_morning_test.py           # 晨报生成并落盘
└── src/
    └── aistock_agent/
        ├── __init__.py
        ├── main.py                   # FastAPI 入口 + lifespan
        ├── config.py                 # pydantic-settings 配置
        ├── constants.py              # SSE/WS 事件类型 / intent 集合 / 错误码 / TOOL_LABELS
        │
        ├── data/                     # 静态数据文件
        │   └── sector_aliases.json   # 板块别名字典（35 标准板块 → 别名列表，快照生成器第一级匹配）
        │
        ├── state/
        │   └── schema.py             # AgentState TypedDict（预加载字段为 NotRequired）
        │
        ├── schemas/                  # 数据模型（Pydantic 对外交互 + TypedDict 内部结构）
        │   ├── chat.py               # ChatRequest / ChatResponse
        │   ├── sse.py                # SSEEvent
        │   ├── agents.py             # 各 Agent 输入/输出 schema
        │   └── snapshot.py           # 快照数据模型（11 TypedDict：SnapshotData / RollingStatsData / ManifestData）
        │
        ├── memory/                   # 持久化记忆模块
        │   ├── checkpointer.py       # LangGraph checkpointer 工厂（MemorySaver 默认）
        │   ├── session_store.py      # 会话历史读写
        │   └── preferences.py        # 用户偏好/自选股记忆
        │
        ├── utils/                    # 通用工具
        │   ├── sse.py                # LangGraph 事件 → SSE 事件映射
        │   ├── parser.py             # LLM 输出解析（parse_intent）
        │   ├── message.py            # 消息提取
        │   ├── report_parser.py      # 双层报告解析（兼容 schema_version 1.0/2.0，规范14）
        │   └── date.py               # 日期/交易日工具
        │
        ├── errors/
        │   └── exceptions.py         # AgentError 体系（预留，当前无抛出点）
        │
        ├── graph/
        │   ├── builder.py            # StateGraph 构建 + compile()
        │   └── routers/
        │       └── intent_router.py  # route_by_intent
        │
        ├── agents/                   # 物理分层：supervisor/ + general/ + workers/
        │   ├── supervisor/node.py    # 意图分类（quick_think）
        │   ├── general/node.py       # 兜底对话（quick_think）
        │   └── workers/
        │       ├── morning.py        # 晨报（deep_think + ReAct + Redis 缓存 + 文件归档，scheduler 触发）
        │       ├── stock.py          # 个股分析（deep_think）
        │       ├── sector.py         # 板块分析（deep_think）
        │       ├── event.py          # 事件传导链（deep_think）
        │       ├── hot_burst.py      # 机构调研热门股（deep_think + ReAct）
        │       ├── wind_leader.py    # 长线风口龙头（deep_think + 定时触发 + 双层输出）
        │       ├── broadcast.py      # 播报生成（deep_think + Node.js 双人播客 + 消费 podcast_brief）
        │       ├── ai_advisor.py     # 智能投顾（deep_think + 消费 display_report）
        │       ├── alert.py          # 异动提醒（deep_think + 三步框架 + cycle 短中长线分类）
        │       ├── review.py         # 复盘归因（deep_think + ReAct + Redis 缓存 + 文件归档，scheduler 触发）
        │       └── iterate.py        # 迭代分析（deep_think，非 ReAct，pipeline + LLM，只读，scheduler 触发）
        │
        ├── tools/                    # LangChain @tool，按数据域分组
        │   ├── base.py               # safe_tool_call 装饰器 + BaseToolMixin + DEGRADED_MESSAGE
        │   ├── stock_tools.py        # get_quote, get_capital_flow, get_profit_forecast
        │   ├── sector_tools.py       # get_leader_stocks, get_wind_leaders
        │   ├── news_tools.py         # search_cls_news, get_news_fulltext, get_cls_news
        │   ├── market_tools.py       # get_global_markets（yfinance 境外行情）
        │   ├── search_tools.py       # tavily_finance_search（Tavily 全网搜索，调 services/tavily.py）
        │   ├── registry.py           # 工具注册中心（get_tools / get_all_tools）
        │   ├── monitor_tools.py      # get_stock_monitor, get_alert_history
        │   ├── tenx_tools.py         # get_tenx_score, get_tenx_top_stocks
        │   ├── graph_tools.py        # get_concepts, get_graph_by_concept
        │   ├── hot_burst_tools.py    # get_hot_burst, get_hot_burst_history
        │   └── review_tools.py       # get_market_summary, get_sector_performance（复盘流水线，review category）
        │
        ├── prompts/                  # 分层对应 agents 目录
        │   ├── supervisor/routing.py
        │   ├── general/system.py     # SYSTEM_PROMPT + GENERAL_PROMPT
        │   └── workers/{morning,stock,sector,event,review,iterate}.py
        │
        ├── services/                 # 全局资源封装
        │   ├── llm.py                # 双模型工厂（get_quick_think / get_deep_think）
        │   ├── data_client.py        # NodeApiClient（node_api 单例）
        │   ├── data_guard.py         # 空数据预检（ensure_data_available + DataCheck，规范13）
        │   ├── redis_pool.py         # Redis 连接池单例（lifespan 管理）
        │   ├── http_client.py        # httpx AsyncClient 单例（lifespan 管理）
        │   ├── cache.py              # 晨报 + 复盘缓存（基于 RedisPool，get/set_cached_briefing + get/set_cached_review）
        │   ├── tavily.py             # Tavily 客户端封装（TavilyService.search）
        │   ├── snapshot_builder.py   # 快照生成器（代码框架 + LLM 4维评估，文件I/O + MA + manifest + 板块匹配）
        │   └── scheduler.py          # APScheduler 定时调度（08:50晨报 / 15:30复盘 / 15:35快照 / 15:40迭代）
        │
        ├── observability/            # 可观测性（零侵入业务）
        │   ├── logging.py            # structlog JSON 日志（setup_logging / get_logger）
        │   ├── callback.py           # TokenUsageCallback / AgentTraceCallback
        │   └── metrics.py            # MetricsCollector 线程安全计数器
        │
        └── api/
            ├── routes.py             # REST 接口 + /health + /health/ready + /skills
            ├── deps.py               # 依赖注入（verify_internal_token / build_initial_state）
            ├── middleware.py         # request_id / access_log / CORS 中间件
            └── ws.py                 # WebSocket 流式接口

tests/
├── conftest.py                       # 共享 fixture（mock_node_api / mock_redis 等）
├── unit/                             # 工具函数单测
├── integration/                      # Agent + Graph 集成测试
└── e2e/                              # HTTP 端到端测试
```

---

## 附录 B：常用命令速查

### 开发服务

```bash
# 启动开发服务（热重载）
uvicorn aistock_agent.main:app --reload --port 8000

# 设置 PYTHONPATH 后运行（如脚本直接 import aistock_agent）
$env:PYTHONPATH = "src"; python scripts/run_morning_test.py
```

### 测试（分层运行）

```bash
pytest tests/ -v                    # 全部测试
pytest tests/unit/ -v               # 仅单元测试（工具函数）
pytest tests/integration/ -v        # 仅集成测试（Agent + Graph）
pytest tests/e2e/ -v                # 仅端到端测试（HTTP 接口）
pytest tests/unit/test_stock_tools.py -v   # 单个文件
pytest tests/unit/test_stock_tools.py::test_get_quote_success -v   # 单个测试
```

### 代码检查

```bash
ruff check src/                     # 代码风格检查
ruff check src/ --fix               # 自动修复（import 排序等）
mypy src/                           # 类型检查（strict 模式）
```

### 图编译验证

```bash
# 验证 LangGraph 图可编译（不启动服务）
python -c "from aistock_agent.graph.builder import compile_graph; compile_graph()"
```

### Git（changer 分支策略）

```bash
# 本地开发在 changer 分支（跟踪 origin/changer，非主分支）
git branch --show-current           # 确认在 changer

# 推送（安全，推到 origin/changer 不污染主分支）
git push

# 同步主分支更新到 changer
git fetch origin
git rebase origin/main              # agent-py 主分支是 main

# 发布：GitHub 创建 PR changer → main
```

### Docker

```bash
docker build -t aistock-agent .
docker run -p 8000:8000 --env-file .env aistock-agent
```

---

*本文件随代码库演进持续更新。新增 Agent / Tool / 规范时同步修订对应章节。*
