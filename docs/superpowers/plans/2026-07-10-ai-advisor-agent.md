# 智能投顾 Agent 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 实现 ai_advisor_agent，从数据库读取已有分析报告，整理汇总后直接在对话中回复用户，节省 token 并提升响应速度。

**架构：** 用户在 App 前端 AI 投顾页面点击快捷指令或提问时，supervisor 识别意图后路由到 ai_advisor_agent。ai_advisor 先查 DB 是否有当日对应报告（morning/wind_leader/hot_burst），有则用 LLM 整理汇总成简洁对话回复；无则降级调用工具获取数据并生成回复。回复直接展示在对话气泡中，不额外跳转页面。scheduler 触发的定时任务流程不受影响。

**技术栈：** Python（LangGraph、LangChain ReAct agent、deep_think LLM、httpx），TypeScript（Express publicRouter），Vue 3（uni-app、Pinia、luch-request）

## 全局约束

- TypeScript：禁止 `any`，用 `unknown`
- Python：禁止 bare `except`，必须有类型注解
- INTENT_SET、VALID_INTENTS、TOOL_LABELS 必须与 intent_router.py 同步
- AgentState 新字段必须为 NotRequired（向后兼容）
- 所有 cron.schedule 必须指定 `{ timezone: 'Asia/Shanghai' }`
- 前端使用 luch-request（非 axios）
- SVG 图标用 SvgIcon 组件，禁止 emoji
- 禁止全量重写，增量修改
- 禁止 TBD / TODO / "implement later" / "add appropriate error handling"
- 每步必须包含完整代码
- **手机端优先：回复必须简洁（200 字以内），用要点式排版，避免大段文字**

---

## 文件结构

| 仓库 | 文件 | 操作 | 职责 |
|------|------|------|------|
| aistock-agent-py | `src/aistock_agent/prompts/workers/ai_advisor.py` | 创建 | ai_advisor 提示词模板 |
| aistock-agent-py | `src/aistock_agent/agents/workers/ai_advisor.py` | 创建 | ai_advisor agent 节点（DB 优先 → ReAct 降级） |
| aistock-agent-py | `tests/unit/test_ai_advisor.py` | 创建 | ai_advisor 单元测试 |
| aistock-agent-py | `src/aistock_agent/constants.py` | 修改 | INTENT_SET 增加 ai_advisor |
| aistock-agent-py | `src/aistock_agent/graph/routers/intent_router.py` | 修改 | VALID_INTENTS 增加 ai_advisor，route_by_intent 增加用户对话路由 |
| aistock-agent-py | `src/aistock_agent/graph/builder.py` | 修改 | 注册 ai_advisor_agent 节点 |
| aistock-agent-py | `src/aistock_agent/prompts/supervisor/routing.py` | 修改 | 路由提示词增加 ai_advisor 描述 |

---

### 任务 1: 创建 ai_advisor 提示词模板

**文件：**
- 创建: `src/aistock_agent/prompts/workers/ai_advisor.py`

**接口：**
- 产出: `AI_ADVISOR_PROMPT` (str) — 供任务 2 的 ai_advisor agent 使用；包含 `{{AVAILABLE_REPORTS}}` 占位符

- [ ] **步骤 1: 创建提示词文件**

```python
# src/aistock_agent/prompts/workers/ai_advisor.py
"""AI 投顾 Agent 提示词 — 智能投顾对话

优先使用已有分析报告整理回复，无报告时降级使用工具获取数据。
手机端显示，回复必须简洁。
"""

AI_ADVISOR_PROMPT = """你是一位专业 AI 投资顾问。根据已有分析报告，为用户提供简洁投资建议。

## 核心原则

1. **简洁优先**：回复控制在 200 字以内，手机端显示
2. **要点式排版**：用「•」列要点，不用大段文字
3. **报告驱动**：优先基于已有报告回答，不要重复报告全部内容，提取关键结论

## 回复格式

1. 一句话结论
2. 2-3 个核心要点
3. 风险提示（如有）

## 可用报告

{{AVAILABLE_REPORTS}}

## 注意

- 不要复述报告全文，提炼关键信息
- 不做无依据预测
- 涉及个股提醒投资风险
- 用中文数字和符号（如「↑」「↓」）让排版更紧凑
"""
```

- [ ] **步骤 2: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/prompts/workers/ai_advisor.py
git commit -m "feat(agent): add ai_advisor prompt template"
```

---

### 任务 2: 实现 ai_advisor agent 节点

**文件：**
- 创建: `src/aistock_agent/agents/workers/ai_advisor.py`

**接口：**
- 消费: `AI_ADVISOR_PROMPT` 来自任务 1；`node_api.get_analysis_report(report_type, report_date)` 来自 `data_client.py`；`get_tools("advisor")` 来自 `tools/registry.py`；`AgentState` 来自 `state/schema.py`；`extract_final_ai_response` 来自 `utils/message.py`；`get_deep_think` 来自 `services/llm.py`
- 产出: `run(state: AgentState) -> dict[str, object]` — 供任务 4 的 graph builder 注册为节点 "ai_advisor_agent"；返回 `{"final_response": str}`

- [ ] **步骤 1: 创建 ai_advisor agent 实现**

```python
# src/aistock_agent/agents/workers/ai_advisor.py
"""AI 投顾 Agent — 智能投顾对话节点

用户对话触发时，优先从数据库读取已有分析报告整理汇总回复。
降级策略：DB 无报告 → 使用工具获取数据 → LLM 生成回复。
模型：deep_think（需要深度分析能力）
回复必须简洁（200 字以内），适合手机端显示。
"""

from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()

# intent → report_type 映射
INTENT_REPORT_MAP: dict[str, str] = {
    "morning": "morning",
    "wind_leader": "wind_leader",
    "hot_burst": "hot_burst",
    "stock": "stock",
    "sector": "sector",
}

# 综合咨询时查询的公共报告类型
_GENERAL_REPORT_TYPES: list[str] = ["morning", "wind_leader", "hot_burst"]

# 报告中文标签
_REPORT_LABELS: dict[str, str] = {
    "morning": "晨报",
    "wind_leader": "风口",
    "hot_burst": "热门股",
    "stock": "个股",
    "sector": "板块",
}

# 报告截取最大字符数（避免超长 prompt）
_REPORT_TRUNCATE_LENGTH = 1500


async def _fetch_relevant_reports(
    intent: str, report_date: str
) -> dict[str, str]:
    """从数据库读取与用户意图相关的分析报告

    Args:
        intent: 用户意图
        report_date: 报告日期 (YYYY-MM-DD)

    Returns:
        报告字典 {report_type: report_text}
    """
    reports: dict[str, str] = {}

    if intent in INTENT_REPORT_MAP:
        report_types_to_query = [INTENT_REPORT_MAP[intent]]
    else:
        report_types_to_query = _GENERAL_REPORT_TYPES

    for report_type in report_types_to_query:
        try:
            data = await node_api.get_analysis_report(report_type, report_date)
            if data and isinstance(data.get("content"), dict):
                text = data["content"].get("text")
                if isinstance(text, str) and text:
                    reports[report_type] = text
        except Exception as e:
            logger.warning(
                "advisor_report_fetch_failed",
                report_type=report_type,
                error=str(e),
            )

    return reports


def _format_available_reports(reports: dict[str, str]) -> str:
    """将报告字典格式化为提示词中的可用报告描述"""
    if not reports:
        return "暂无当日分析报告，请使用工具获取最新数据后回答用户问题。"

    parts: list[str] = []
    for report_type, text in reports.items():
        label = _REPORT_LABELS.get(report_type, report_type)
        truncated = text[:_REPORT_TRUNCATE_LENGTH] + ("..." if len(text) > _REPORT_TRUNCATE_LENGTH else "")
        parts.append(f"### {label}\n{truncated}")

    return "\n\n".join(parts)


async def run(state: AgentState) -> dict[str, object]:
    """智能投顾：优先从 DB 读取报告，降级使用工具获取数据

    流程：
    1. 根据 state.intent 查询数据库中的相关报告
    2. 如果有报告：用 LLM 基于报告整理汇总回复（省 token）
    3. 如果无报告：用 ReAct Agent 调用工具获取数据后回复
    4. 回复直接展示在对话气泡中，简洁要点式排版
    """
    try:
        intent = state.get("intent", "general") or "general"
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")

        # 步骤 1: 查询数据库
        reports = await _fetch_relevant_reports(intent, report_date)
        logger.info(
            "advisor_reports_fetched",
            intent=intent,
            report_date=report_date,
            reports_found=list(reports.keys()),
        )

        # 步骤 2: 构造提示词
        available_reports_text = _format_available_reports(reports)
        prompt = AI_ADVISOR_PROMPT.replace("{{AVAILABLE_REPORTS}}", available_reports_text)

        if reports:
            # 有报告：直接用 LLM 整理汇总（省 token，快速响应）
            llm = get_deep_think()
            response = await llm.ainvoke([
                SystemMessage(content=prompt),
                *state.get("messages", [])[-5:],
            ])

            final_response = response.content if isinstance(response.content, str) else str(response.content)
            logger.info("advisor_response_from_reports", has_report=True, intent=intent)
        else:
            # 无报告：用 ReAct Agent 调用工具获取数据
            llm = get_deep_think()
            tools = get_tools("advisor")
            agent = create_react_agent(llm, tools)

            result = await agent.ainvoke({
                "messages": [
                    SystemMessage(content=prompt),
                    *state.get("messages", [])[-5:],
                ]
            })

            final_response = extract_final_ai_response(result.get("messages", []))
            logger.info("advisor_response_from_tools", has_report=False, intent=intent)

        return {"final_response": final_response}

    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="ai_advisor",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "智能投顾暂时不可用，请稍后重试"}
```

- [ ] **步骤 2: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/agents/workers/ai_advisor.py
git commit -m "feat(agent): add ai_advisor agent implementation"
```

---

### 任务 3: 注册 advisor 工具集

**文件：**
- 修改: `src/aistock_agent/tools/stock_tools.py`
- 修改: `src/aistock_agent/tools/market_tools.py`
- 修改: `src/aistock_agent/tools/news_tools.py`
- 修改: `src/aistock_agent/tools/sector_tools.py`
- 修改: `src/aistock_agent/tools/hot_burst_tools.py`
- 修改: `src/aistock_agent/tools/search_tools.py`

**接口：**
- 消费: 任务 2 中 `get_tools("advisor")` 调用
- 产出: `get_tools("advisor")` 返回 advisor 分类下的工具列表

- [ ] **步骤 1: 在各工具模块末尾追加 advisor 分类注册**

在 `src/aistock_agent/tools/stock_tools.py` 底部追加：

```python
# advisor agent 复用
register("advisor", get_quote)
register("advisor", get_capital_flow)
register("advisor", get_profit_forecast)
register("advisor", search_cls_news)
```

在 `src/aistock_agent/tools/market_tools.py` 底部追加：

```python
# advisor agent 复用
register("advisor", get_global_markets)
```

在 `src/aistock_agent/tools/news_tools.py` 底部追加：

```python
# advisor agent 复用
register("advisor", get_cls_news)
```

在 `src/aistock_agent/tools/sector_tools.py` 底部追加：

```python
# advisor agent 复用
register("advisor", get_leader_stocks)
```

在 `src/aistock_agent/tools/hot_burst_tools.py` 底部追加：

```python
# advisor agent 复用
register("advisor", get_hot_burst)
register("advisor", get_hot_burst_history)
```

在 `src/aistock_agent/tools/search_tools.py` 底部追加：

```python
# advisor agent 复用
register("advisor", tavily_finance_search)
```

- [ ] **步骤 2: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/tools/stock_tools.py src/aistock_agent/tools/market_tools.py src/aistock_agent/tools/news_tools.py src/aistock_agent/tools/sector_tools.py src/aistock_agent/tools/hot_burst_tools.py src/aistock_agent/tools/search_tools.py
git commit -m "feat(tools): register advisor category in tool modules"
```

---

### 任务 4: 更新路由和图拓扑

**文件：**
- 修改: `src/aistock_agent/constants.py`
- 修改: `src/aistock_agent/graph/routers/intent_router.py`
- 修改: `src/aistock_agent/graph/builder.py`
- 修改: `src/aistock_agent/prompts/supervisor/routing.py`

**接口：**
- 消费: `ai_advisor.run` 来自任务 2
- 产出: graph 中 "ai_advisor_agent" 节点可被 supervisor 通过条件边路由到

- [ ] **步骤 1: 更新 constants.py — INTENT_SET 增加 ai_advisor**

在 INTENT_SET 的 frozenset 中增加 `"ai_advisor"`：

```python
INTENT_SET = frozenset({"morning", "stock", "sector", "event", "wind_leader", "hot_burst", "broadcast", "alert", "ai_advisor", "general"})
```

- [ ] **步骤 2: 更新 intent_router.py — 增加用户对话路由逻辑**

关键变更：当 `trigger_source="user"` 且 intent 不是 broadcast/general 时，路由到 ai_advisor_agent，让 agent 从 DB 读报告后整理回复。

```python
# src/aistock_agent/graph/routers/intent_router.py
"""条件边函数 — supervisor 输出后根据 intent 路由到对应 Agent"""

from aistock_agent.state.schema import AgentState

VALID_INTENTS = {"morning", "stock", "sector", "event", "wind_leader", "broadcast", "hot_burst", "alert", "ai_advisor", "general"}


def route_by_intent(state: AgentState) -> str:
    """根据 state.intent 路由到对应 Agent 节点

    当 trigger_source="user" 时，非 general/broadcast 的意图路由到 ai_advisor_agent，
    从 DB 读取已有报告整理回复（省 token）。
    """
    intent = state.get("intent", "general") or "general"
    if intent not in VALID_INTENTS:
        intent = "general"

    # 用户对话走 ai_advisor（复用 DB 报告，整理后直接回复）
    trigger_source = state.get("trigger_source")
    if trigger_source == "user" and intent not in ("general", "broadcast"):
        return "ai_advisor_agent"

    node_map = {
        "morning": "morning_agent",
        "stock": "stock_analyst",
        "sector": "sector_analyst",
        "event": "event_analyst",
        "wind_leader": "wind_leader_agent",
        "broadcast": "broadcast_agent",
        "general": "general_agent",
        "hot_burst": "hot_burst_agent",
        "alert": "alert_agent",
        "ai_advisor": "ai_advisor_agent",
    }
    return node_map[intent]
```

- [ ] **步骤 3: 更新 builder.py — 注册 ai_advisor_agent 节点**

在 `src/aistock_agent/graph/builder.py` 中：

导入区增加：
```python
from aistock_agent.agents.workers import ai_advisor as ai_advisor_agent
```

`build_graph()` 函数中注册节点（在现有 agent 节点之后）：
```python
graph.add_node("ai_advisor_agent", ai_advisor_agent.run)
```

`agent_nodes` 列表增加 `"ai_advisor_agent"`：
```python
agent_nodes = [
    "morning_agent", "stock_analyst", "sector_analyst",
    "event_analyst", "wind_leader_agent", "broadcast_agent", "general_agent",
    "hot_burst_agent", "alert_agent", "ai_advisor_agent",
]
```

- [ ] **步骤 4: 更新 routing.py — supervisor 路由提示词增加 ai_advisor 描述**

```python
# src/aistock_agent/prompts/supervisor/routing.py
"""路由分类提示词 — Supervisor 用"""

ROUTING_PROMPT = """你是一个意图分类器。分析用户消息，判断其意图类别。

只能输出以下类别之一：
- morning：用户想看晨报/早报/晚报/市场综述/宏观分析
- stock：用户想分析某只个股（包含6位股票代码）
- sector：用户想看板块分析/龙头股/概念板块
- event：用户想了解事件传导/政策影响/利好利空
- wind_leader：用户想看长线风口/风口龙头/风口板块
- broadcast：用户想听播报/早点听/双人播报
- hot_burst：机构调研热门股、机构共振、热门机构票、共振热门股
- alert：用户想看异动提醒/异动事件/涨跌异动/量价异动/公告研判
- ai_advisor：用户综合咨询投资建议、询问今日市场总结、多角度分析请求
- general：其他对话或不明确的意图

只输出类别名，不要解释。例如：stock
"""
```

- [ ] **步骤 5: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/constants.py src/aistock_agent/graph/routers/intent_router.py src/aistock_agent/graph/builder.py src/aistock_agent/prompts/supervisor/routing.py
git commit -m "feat(graph): add ai_advisor_agent node + user-trigger routing"
```

---

### 任务 5: 更新文档

**文件：**
- 修改: `AGENTS.md` (aistock-agent-py)
- 修改: `changelog-pending.md` (aistock-agent-py)

- [ ] **步骤 1: 更新 aistock-agent-py AGENTS.md**

在 Agent 列表中增加 ai_advisor_agent 条目：
- 意图：`ai_advisor`
- 功能：用户对话时从 DB 读取报告整理汇总后直接回复，省 token
- 降级：无报告时调用 advisor 工具集获取数据
- 路由规则：trigger_source="user" 且 intent 不是 general/broadcast 时路由到 ai_advisor_agent
- 回复风格：简洁要点式（200 字以内），适合手机端

- [ ] **步骤 2: 更新 changelog-pending.md**

追加：
```markdown
## 2026-07-10

### 新增智能投顾 Agent（ai_advisor_agent）
- **文件**: `src/aistock_agent/agents/workers/ai_advisor.py`（新建）、`src/aistock_agent/prompts/workers/ai_advisor.py`（新建）
- **功能**: 用户对话触发时，从 DB 读取已有分析报告整理汇总后直接回复对话气泡，不跳转页面
- **降级**: DB 无报告时调用 advisor 工具集获取数据
- **路由**: trigger_source="user" 且 intent 不是 general/broadcast 时路由到 ai_advisor_agent
- **回复风格**: 简洁要点式（200 字以内），适合手机端
```

- [ ] **步骤 3: 提交**

```bash
cd d:/aistock/aistock-agent-py
git add AGENTS.md changelog-pending.md
git commit -m "docs: update AGENTS.md and changelog for ai_advisor_agent"
```

---

## 实施后验证

### 服务器端验证

1. 重新部署 aistock-agent-py 到服务器
2. 用 curl 测试 AI 投顾对话：
```bash
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: $TOKEN" \
  -d '{"message": "帮我分析一下今天的市场", "trigger_source": "user"}'
```
3. 检查日志确认 ai_advisor_agent 被路由到，且从 DB 读取了报告

### 前端验证

1. 启动 H5 开发服务器：`npm run dev:h5`
2. 进入 AI 投顾页面
3. 点击快捷指令（如"今日晨报""长线风口"等）
4. 验证：
   - 回复基于已有报告（查看日志确认 `has_report=True`）
   - 回复简洁要点式，200 字以内
   - 回复直接显示在对话气泡中，无需额外跳转

## 自检清单

- [x] 规范覆盖：ai_advisor_agent 实现（任务 2）、工具注册（任务 3）、路由/图拓扑（任务 4）、文档（任务 5）
- [x] 无占位符：所有步骤包含完整代码
- [x] 类型一致性：AgentState 字段、intent 值、节点名称在所有任务中一致
- [x] 全局约束：禁止 `any`、新字段 NotRequired、INTENT_SET 同步
- [x] 手机端优先：回复简洁要点式，不跳转页面
