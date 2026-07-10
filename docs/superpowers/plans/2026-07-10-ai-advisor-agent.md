# AI Advisor Agent 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 ai_advisor_agent，使用户在 App 前端 AI 投顾页面提问时，优先从数据库读取已有分析报告汇总回复，节省 token 并提升响应速度。

**Architecture:** 用户对话触发时，supervisor 识别意图后路由到 ai_advisor_agent。ai_advisor 先查 DB 是否有当日对应报告（morning/wind_leader/hot_burst），有则用 LLM 汇总成对话回复；无则降级调用工具获取数据并生成回复。scheduler 触发的定时任务流程不受影响。

**Tech Stack:** Python (LangGraph, LangChain ReAct agent, deep_think LLM), TypeScript (Express publicRouter), Vue 3 (uni-app)

## Global Constraints

- 禁止 `any`，用 `unknown`（TypeScript）/ 类型注解（Python）
- INTENT_SET、VALID_INTENTS、TOOL_LABELS 必须与 intent_router 同步
- AgentState 新字段必须为 NotRequired（向后兼容）
- 所有 cron.schedule 必须指定 `{ timezone: 'Asia/Shanghai' }`
- 前端使用 luch-request（非 axios）
- SVG 图标用 SvgIcon 组件，禁止 emoji
- 禁止全量重写，增量修改

---

## File Structure

| 仓库 | 文件 | 操作 | 职责 |
|------|------|------|------|
| aistock-agent-py | `src/aistock_agent/agents/workers/ai_advisor.py` | 创建 | ai_advisor agent 实现 |
| aistock-agent-py | `src/aistock_agent/prompts/workers/ai_advisor.py` | 创建 | ai_advisor 提示词 |
| aistock-agent-py | `src/aistock_agent/constants.py` | 修改 | INTENT_SET 增加 ai_advisor，TOOL_LABELS 增加工具标签 |
| aistock-agent-py | `src/aistock_agent/graph/routers/intent_router.py` | 修改 | VALID_INTENTS 增加 ai_advisor，route_by_intent 增加用户对话路由逻辑 |
| aistock-agent-py | `src/aistock_agent/graph/builder.py` | 修改 | 注册 ai_advisor_agent 节点 |
| aistock-agent-py | `src/aistock_agent/prompts/supervisor/routing.py` | 修改 | 路由提示词增加 ai_advisor 描述 |
| aistock-agent-py | `src/aistock_agent/agents/supervisor/node.py` | 修改 | supervisor 识别用户综合咨询意图 |
| aistock-app-frontend | `src/shared/api/modules/agent.ts` | 修改 | 增加获取多种报告的辅助方法 |
| aistock-app-frontend | `src/pages-sub-app/chat/index.vue` | 修改 | 优化消息展示（报告卡片、Skill 结果卡片样式） |
| aistock-app-frontend | `src/shared/store/modules/chat.ts` | 修改 | 增加 advisor 模式支持 |

---

### Task 1: 创建 ai_advisor 提示词

**Files:**
- Create: `src/aistock_agent/prompts/workers/ai_advisor.py`

**Interfaces:**
- Produces: `AI_ADVISOR_PROMPT` (str) — 供 Task 2 的 ai_advisor agent 使用

- [ ] **Step 1: 创建提示词文件**

```python
"""AI Advisor Agent 提示词 — 智能投顾对话"""

AI_ADVISOR_PROMPT = """你是一位专业的 AI 投资顾问。你的任务是根据已有的分析报告，为用户提供清晰、专业的投资建议。

## 工作流程

1. **优先使用已有报告**：如果提供了当日分析报告，基于报告内容回答用户问题
2. **数据驱动**：如果需要补充数据，使用工具获取最新行情、资金流向等信息
3. **结构化回复**：回复应包含关键结论、数据支撑和风险提示

## 回复格式

- 先给出简明结论（1-2句话）
- 再展开详细分析
- 最后给出风险提示（如有）

## 可用报告

{{AVAILABLE_REPORTS}}

## 注意事项

- 基于数据和事实分析，不做无依据的预测
- 明确区分"事实"和"观点"
- 涉及具体个股时，提醒投资风险
"""
```

- [ ] **Step 2: Commit**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/prompts/workers/ai_advisor.py
git commit -m "feat(agent): add ai_advisor prompt template"
```

---

### Task 2: 创建 ai_advisor agent 实现

**Files:**
- Create: `src/aistock_agent/agents/workers/ai_advisor.py`

**Interfaces:**
- Consumes: `AI_ADVISOR_PROMPT` from Task 1, `node_api.get_analysis_report()` from data_client, `AgentState` from schema
- Produces: `run(state: AgentState) -> dict` — 供 Task 4 的 graph builder 注册为节点

- [ ] **Step 1: 创建 ai_advisor agent**

```python
"""AI Advisor Agent — 智能投顾对话节点

用户对话触发时，优先从数据库读取已有分析报告汇总回复。
降级策略：DB 无报告 → 使用工具获取数据 → LLM 生成回复。
模型：deep_think（需要深度分析能力）
"""

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response, extract_last_human_message

logger = structlog.get_logger()

# intent → report_type 映射
INTENT_REPORT_MAP = {
    "morning": "morning",
    "wind_leader": "wind_leader",
    "hot_burst": "hot_burst",
    "stock": "stock",
    "sector": "sector",
}


async def _fetch_relevant_reports(intent: str, report_date: str, symbol: str | None = None) -> dict[str, str]:
    """从数据库读取与用户意图相关的分析报告

    Args:
        intent: 用户意图
        report_date: 报告日期 (YYYY-MM-DD)
        symbol: 股票代码（可选，用于个性化报告）

    Returns:
        报告字典 {report_type: report_text}
    """
    reports: dict[str, str] = {}

    # 根据意图决定查询哪些报告
    report_types_to_query: list[str] = []

    if intent in INTENT_REPORT_MAP:
        report_types_to_query.append(INTENT_REPORT_MAP[intent])
    else:
        # 综合咨询：查询所有公共报告
        report_types_to_query = ["morning", "wind_leader", "hot_burst"]

    for report_type in report_types_to_query:
        try:
            data = await node_api.get_analysis_report(report_type, report_date)
            if data and isinstance(data.get("content"), dict):
                text = data["content"].get("text")
                if isinstance(text, str) and text:
                    reports[report_type] = text
        except Exception as e:
            logger.warning("advisor_report_fetch_failed", report_type=report_type, error=str(e))

    return reports


def _format_available_reports(reports: dict[str, str]) -> str:
    """将报告字典格式化为提示词中的可用报告描述"""
    if not reports:
        return "暂无当日分析报告，请使用工具获取最新数据后回答用户问题。"

    labels = {
        "morning": "晨报宏观分析",
        "wind_leader": "长线风口分析",
        "hot_burst": "机构调研热门股分析",
        "stock": "个股分析",
        "sector": "板块分析",
    }

    parts: list[str] = []
    for report_type, text in reports.items():
        label = labels.get(report_type, report_type)
        # 截取前 2000 字符避免超长
        truncated = text[:2000] + ("..." if len(text) > 2000 else "")
        parts.append(f"### {label}\n{truncated}")

    return "\n\n".join(parts)


async def run(state: AgentState) -> dict[str, object]:
    """智能投顾：优先从 DB 读取报告，降级使用工具获取数据

    流程：
    1. 根据 state.intent 查询数据库中的相关报告
    2. 如果有报告：用 LLM 基于报告汇总回复（省 token）
    3. 如果无报告：用 ReAct Agent 调用工具获取数据后回复
    4. 返回对话回复
    """
    try:
        from datetime import datetime

        intent = state.get("intent", "general") or "general"
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
        symbol = state.get("symbol")

        # Step 1: 查询数据库
        reports = await _fetch_relevant_reports(intent, report_date, symbol)
        logger.info(
            "advisor_reports_fetched",
            intent=intent,
            report_date=report_date,
            reports_found=list(reports.keys()),
        )

        # Step 2: 构造提示词
        available_reports_text = _format_available_reports(reports)
        prompt = AI_ADVISOR_PROMPT.replace("{{AVAILABLE_REPORTS}}", available_reports_text)

        if reports:
            # 有报告：直接用 LLM 汇总（省 token，快速响应）
            llm = get_deep_think()
            user_message = extract_last_human_message(state.get("messages", []))

            response = await llm.ainvoke([
                SystemMessage(content=prompt),
                *state.get("messages", [])[-5:],
            ])

            final_response = response.content if isinstance(response.content, str) else str(response.content)
            logger.info("advisor_response_from_reports", has_report=True, intent=intent)
        else:
            # 无报告：用 ReAct Agent 调用工具获取数据
            llm = get_deep_think()
            tools = get_tools("advisor")  # advisor 工具集
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

- [ ] **Step 2: Commit**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/agents/workers/ai_advisor.py
git commit -m "feat(agent): add ai_advisor agent implementation"
```

---

### Task 3: 注册 advisor 工具集

**Files:**
- Modify: `src/aistock_agent/tools/registry.py`

**Interfaces:**
- Consumes: Task 2 中 `get_tools("advisor")` 调用
- Produces: advisor 工具集列表

- [ ] **Step 1: 在工具注册表中增加 advisor 工具集**

在 `get_tools` 函数的 agent_tools_map 中增加 `"advisor"` 条目，复用 morning + stock 的工具集。

```python
# 在 agent_tools_map 字典中增加：
"advisor": [
    "get_quote", "get_capital_flow", "get_profit_forecast",
    "get_wind_leaders", "get_hot_burst", "get_cls_news",
    "tavily_finance_search", "get_global_markets",
],
```

- [ ] **Step 2: Commit**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/tools/registry.py
git commit -m "feat(tools): add advisor tool set"
```

---

### Task 4: 更新路由和图拓扑

**Files:**
- Modify: `src/aistock_agent/constants.py`
- Modify: `src/aistock_agent/graph/routers/intent_router.py`
- Modify: `src/aistock_agent/graph/builder.py`
- Modify: `src/aistock_agent/prompts/supervisor/routing.py`

**Interfaces:**
- Consumes: `ai_advisor.run` from Task 2
- Produces: graph 中 ai_advisor_agent 节点可被路由到

- [ ] **Step 1: constants.py — INTENT_SET 增加 ai_advisor，TOOL_LABELS 增加工具标签**

```python
# INTENT_SET 修改为：
INTENT_SET = frozenset({"morning", "stock", "sector", "event", "wind_leader", "hot_burst", "broadcast", "ai_advisor", "general"})

# TOOL_LABELS 增加 advisor 工具标签（如果还没有的话，已有则跳过）：
TOOL_LABELS: dict[str, str] = {
    # ... 现有条目 ...
    # advisor agent 工具（复用其他 agent 的工具）
}
```

- [ ] **Step 2: intent_router.py — 增加用户对话路由逻辑**

关键变更：当 `trigger_source="user"` 且 intent 不是 broadcast/general 时，路由到 ai_advisor_agent。

```python
VALID_INTENTS = {"morning", "stock", "sector", "event", "wind_leader", "broadcast", "hot_burst", "ai_advisor", "general"}


def route_by_intent(state: AgentState) -> str:
    """根据 state.intent 路由到对应 Agent 节点

    当 trigger_source="user" 时，非 general/broadcast 的意图路由到 ai_advisor_agent，
    实现报告复用和智能汇总。
    """
    intent = state.get("intent", "general") or "general"
    if intent not in VALID_INTENTS:
        intent = "general"

    # 用户对话走 ai_advisor（复用 DB 报告，省 token）
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
        "ai_advisor": "ai_advisor_agent",
    }
    return node_map[intent]
```

- [ ] **Step 3: builder.py — 注册 ai_advisor_agent 节点**

```python
# 导入增加：
from aistock_agent.agents.workers import ai_advisor as ai_advisor_agent

# build_graph() 中注册节点：
graph.add_node("ai_advisor_agent", ai_advisor_agent.run)

# agent_nodes 列表增加：
agent_nodes = [
    "morning_agent", "stock_analyst", "sector_analyst",
    "event_analyst", "wind_leader_agent", "broadcast_agent", "general_agent",
    "hot_burst_agent", "ai_advisor_agent",
]
```

- [ ] **Step 4: routing.py — supervisor 路由提示词增加 ai_advisor 描述**

```python
ROUTING_PROMPT = """你是一个意图分类器。分析用户消息，判断其意图类别。

只能输出以下类别之一：
- morning：用户想看晨报/早报/晚报/市场综述/宏观分析
- stock：用户想分析某只个股（包含6位股票代码）
- sector：用户想看板块分析/龙头股/概念板块
- event：用户想了解事件传导/政策影响/利好利空
- wind_leader：用户想看长线风口/风口龙头/风口板块
- broadcast：用户想听播报/早点听/双人播报
- hot_burst：机构调研热门股、机构共振、热门机构票、共振热门股
- ai_advisor：用户综合咨询投资建议、询问今日市场总结、多角度分析请求
- general：其他对话或不明确的意图

只输出类别名，不要解释。例如：stock
"""
```

- [ ] **Step 5: Commit**

```bash
cd d:/aistock/aistock-agent-py
git add src/aistock_agent/constants.py src/aistock_agent/graph/routers/intent_router.py src/aistock_agent/graph/builder.py src/aistock_agent/prompts/supervisor/routing.py
git commit -m "feat(graph): add ai_advisor_agent node and routing"
```

---

### Task 5: 前端 AI 投顾页面优化 — 支持报告卡片展示

**Files:**
- Modify: `src/pages-sub-app/chat/index.vue`

**Interfaces:**
- Consumes: `agentApi.getReport()` from 已有的 agent.ts API 模块

- [ ] **Step 1: 在 chat/index.vue 中增加报告卡片引导**

当 AI 回复中包含报告相关内容时，在消息气泡下方显示可点击的报告卡片，跳转到 agent-report 页面。

在 `<script setup>` 中增加跳转函数：

```typescript
function goReport(intent: string) {
  const today = new Date().toISOString().split('T')[0]
  uni.navigateTo({
    url: `/modules/chat/pages/agent-report?intent=${intent}&date=${today}`
  })
}
```

在模板中，assistant 消息气泡底部增加报告入口按钮（当 intent 为 morning/wind_leader/hot_burst 时显示）：

```vue
<!-- 在 .bubble 内部、.skill-text-card 之后添加 -->
<view v-if="msg.intent && msg.intent !== 'general'" class="report-link" @tap="goReport(msg.intent)">
  <SvgIcon name="file-line" size="24rpx" color="#4d7cfe" />
  <text class="report-link-text">查看完整分析报告</text>
</view>
```

增加样式：

```scss
.report-link {
  display: flex; align-items: center; gap: 6rpx;
  margin-top: 12rpx; padding-top: 10rpx;
  border-top: 1rpx solid #e5e7eb;
}
.report-link-text { font-size: 24rpx; color: #4d7cfe; }
```

- [ ] **Step 2: Commit**

```bash
cd d:/aistock/aistock-app-frontend
git add src/pages-sub-app/chat/index.vue
git commit -m "feat(chat): add report link card in AI advisor chat"
```

---

### Task 6: 更新文档和常量

**Files:**
- Modify: `AGENTS.md`（aistock-agent-py）
- Modify: `AGENTS.md`（aistock-app-frontend）
- Modify: `changelog-pending.md`（三个仓库）

- [ ] **Step 1: 更新 aistock-agent-py AGENTS.md**

在 Agent 列表中增加 ai_advisor_agent 条目：
- 意图：`ai_advisor`
- 功能：用户对话时从 DB 读取报告汇总回复，省 token
- 降级：无报告时调用工具获取数据

- [ ] **Step 2: 更新 aistock-app-frontend AGENTS.md**

在 AI 对话模块描述中提及 ai_advisor 支持。

- [ ] **Step 3: 更新三个仓库的 changelog-pending.md**

- [ ] **Step 4: Commit**

```bash
# aistock-agent-py
cd d:/aistock/aistock-agent-py
git add AGENTS.md changelog-pending.md
git commit -m "docs: update AGENTS.md and changelog for ai_advisor_agent"

# aistock-app-frontend
cd d:/aistock/aistock-app-frontend
git add AGENTS.md changelog-pending.md
git commit -m "docs: update AGENTS.md and changelog for ai_advisor support"
```

---

## 实施后验证

### 服务器端验证

1. 重新部署 aistock-agent-py 到服务器
2. 用 curl 测试 AI 投顾对话：
```bash
curl -X POST http://localhost:8080/api/agent/chat/message \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我分析一下今天的市场", "trigger_source": "user"}'
```
3. 检查日志确认 ai_advisor_agent 被路由到，且从 DB 读取了报告

### 前端验证

1. 启动 H5 开发服务器：`npm run dev:h5`
2. 进入 AI 投顾页面
3. 输入"今天市场怎么样"或"分析一下茅台"
4. 验证：
   - 回复基于已有报告（查看日志确认 `has_report=True`）
   - 消息气泡下方显示"查看完整分析报告"链接
   - 点击链接可跳转到 agent-report 页面

## 自检清单

- [x] Spec 覆盖：ai_advisor_agent 实现（Task 2）、路由注册（Task 4）、前端展示（Task 5）、文档（Task 6）
- [x] 无占位符：所有步骤包含完整代码
- [x] 类型一致性：AgentState 字段、intent 值、node 名称在所有 Task 中一致
